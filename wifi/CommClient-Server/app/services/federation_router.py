"""
Federation chain router — message-id dedup + hop-limited flood.

The existing federation layer (federated_emit.py + federation_service.py)
assumes a full mesh: every server directly sees every other server. That
breaks when the topology is a chain — A ↔ B ↔ C ↔ D where A can't reach
D directly. Without forwarding logic the emit from A gets fanned out to
B only; B never retries to reach C or D on A's behalf.

This module adds minimal transit routing:

  * Each message carries a short `message_id` and a `hop_count`.
  * A ``/api/federation/emit`` receiver that isn't the target forwards
    to all of its own peers with ``hop_count+1``. Loop protection comes
    from a recent-message-id cache (60s TTL) — if we see the same id
    twice, we drop it.
  * A hard ``MAX_HOPS`` (default 8) bounds worst-case amplification.
  * On successful local delivery, we remember the originating peer so
    future emits to the same user can go direct instead of flooding.

Why flood-based instead of distance-vector:
  * Needs zero gossip infrastructure. Each server just acts on what
    arrives.
  * Converges on first success — the target server caches the origin
    via the existing `federated_emit.remember_origin()` hook, so the
    second message for the same user skips the flood.
  * Worst-case N-way amplification per message is acceptable for a
    LAN mesh with ~10s of servers; chat traffic is low-volume.
"""

from __future__ import annotations

import collections
import time
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


# Default ceiling on hop_count. Overridable via env if a deeper chain is
# ever needed; 8 handles any realistic LAN topology while keeping amplification
# bounded (with N=20 peers the worst-case message count is 20^8 ≈ 2.5e10,
# but the dedup cache prunes before that so real amplification is bounded
# by the number of servers that haven't yet seen the message — i.e. N).
# All caps are env-tunable so LAN-scale deployments (100+ servers) can
# raise them without code changes. Defaults fit the 1-100 server profile;
# see README § "Scaling profiles" for recommended values at each scale.
import os as _os_caps

def _env_int(name: str, default: int) -> int:
    try:
        v = _os_caps.environ.get(name, "")
        return max(1, int(v)) if v else default
    except ValueError:
        return default

def _env_float(name: str, default: float) -> float:
    try:
        v = _os_caps.environ.get(name, "")
        return max(0.1, float(v)) if v else default
    except ValueError:
        return default

MAX_HOPS_DEFAULT = _env_int("HELEN_FEDERATION_MAX_HOPS", 8)
MESSAGE_ID_TTL_SEC = _env_float("HELEN_FEDERATION_DEDUP_TTL_SEC", 60.0)
MESSAGE_ID_CACHE_MAX = _env_int("HELEN_FEDERATION_DEDUP_MAX", 10_000)


class SeenMessageCache:
    """Ring-buffer-ish dedup cache. message_id → first_seen_ts.

    We check membership on every incoming federated emit; if present and
    not yet expired, the message is a duplicate (arrived via two or more
    peers during the flood) and must be dropped.
    """

    def __init__(self, ttl_seconds: float = MESSAGE_ID_TTL_SEC,
                 max_entries: int = MESSAGE_ID_CACHE_MAX) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        # OrderedDict gives us O(1) eviction of oldest entries.
        self._seen: "collections.OrderedDict[str, float]" = collections.OrderedDict()

    def seen_and_record(self, message_id: str) -> bool:
        """Returns True if this id was already in the cache (i.e. duplicate
        that should be dropped). Records it as seen otherwise."""
        if not message_id:
            # No id means legacy (pre-chain) emit — treat as always-fresh.
            # Flood-amplification is bounded by hop_count anyway.
            return False
        now = time.time()
        self._prune(now)
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return False

    def _prune(self, now: float) -> None:
        # Expire anything older than TTL from the left (oldest insertion).
        cutoff = now - self._ttl
        # OrderedDict is insertion-ordered, so pop from left while stale.
        for _ in range(len(self._seen)):
            try:
                key, ts = next(iter(self._seen.items()))
            except StopIteration:
                return
            if ts >= cutoff:
                return
            self._seen.pop(key, None)


seen_cache = SeenMessageCache()


def next_message_id() -> str:
    """Short but unguessable id. We prefix with the current ms timestamp
    so a glance at a log tells you roughly when the message originated
    without parsing the dedup cache."""
    import secrets as _s
    return f"{int(time.time() * 1000):x}-{_s.token_hex(6)}"


def resolve_max_hops() -> int:
    import os as _os
    try:
        return max(1, int(_os.environ.get("HELEN_FEDERATION_MAX_HOPS", "") or MAX_HOPS_DEFAULT))
    except ValueError:
        return MAX_HOPS_DEFAULT


DHT_FORWARD_K = _env_int("HELEN_FEDERATION_FORWARD_K", 20)


async def forward_to_all_peers(
    *,
    target_user_id: str,
    event: str,
    payload: dict[str, Any],
    message_id: str,
    hop_count: int,
    exclude_server_ids: set[str] | None = None,
) -> int:
    """Forward the emit to peers closest (by XOR distance) to the
    target user — Kademlia-style routing. Bounded fan-out of K peers
    per hop converts the previous O(N) flood into O(K · log N) total
    network traffic per first-contact, which is what makes 1k-10k
    server meshes practical.

    Falls back to "fan-out to every known peer" when the routing
    table has fewer entries than K, preserving the small-cluster
    behavior that the unit tests rely on.

    Returns the number of peers we *attempted* (not that acknowledged
    delivery — acknowledgement just means the peer accepted the forward).
    Caller is responsible for checking ``hop_count < MAX_HOPS`` before
    invoking this.
    """
    import asyncio as _asyncio
    from app.services.federation_service import federation_service
    from app.services.peer_registry import peer_registry
    from app.services.dht_kademlia import get_routing_table

    exclude = exclude_server_ids or set()
    peers = await peer_registry.list(include_stale=False)
    available = [p for p in peers if p.server_id not in exclude]
    if not available:
        return 0

    # If the cluster is small (≤ K peers), keep the legacy "send to
    # everyone" behavior — it converges fastest and the dedup cache
    # covers the overhead. Above K, switch to closest-K routing so
    # the per-hop traffic stays bounded as the mesh grows.
    if len(available) <= DHT_FORWARD_K:
        targets = available
    else:
        rt = get_routing_table()
        # Compute XOR distance between every available peer and the
        # target user_id. We don't have the user's "home server" id, so
        # we treat the user_id itself as a key in the same XOR space —
        # this gives stable closest-K targeting that converges to the
        # owning peer as more peers learn the route via remember_origin.
        candidate_ids = {p.server_id for p in available}
        # Restrict the routing table's "closest" picks to peers we
        # currently believe are alive (peer_registry).
        closest_ids = [
            sid for sid in rt.closest(target_user_id, k=DHT_FORWARD_K * 3)
            if sid in candidate_ids
        ][:DHT_FORWARD_K]
        if not closest_ids:
            # Routing table doesn't yet contain any of the live peers
            # (cold start) — fall back to legacy fan-out.
            targets = available
        else:
            id_to_peer = {p.server_id: p for p in available}
            targets = [id_to_peer[sid] for sid in closest_ids]
        logger.info("federation_forward_dht",
                    available=len(available),
                    targets=len(targets),
                    table_size=rt.size(),
                    target_user_id=target_user_id[:12],
                    message_id=message_id)

    extra = {
        "message_id": message_id,
        "hop_count": hop_count,
        "max_hops": resolve_max_hops(),
    }

    # Approximate body size once so every per-peer bump uses the same value.
    # The real body built inside federation_service may add a few bytes for
    # the hop envelope — close enough for a counter.
    import json as _json
    try:
        approx_bytes = len(_json.dumps({
            "target_user_id": target_user_id, "event": event,
            "payload": payload, **extra,
        }))
    except Exception:
        approx_bytes = 0

    from app.services import federation_metrics as _metrics

    # Circuit breaker + retry — wrap each peer call in `with_retry` so a
    # flaky peer doesn't cost a connect-timeout on every fan-out, and a
    # transient network blip survives one retry. State per peer is tracked
    # in federation_resilience; opened breakers skip until cooldown elapses.
    from app.services import federation_resilience as _resilience

    async def _send(peer) -> int:
        async def _attempt():
            return await federation_service.emit_to_remote_user(
                peer.server_id, target_user_id, event, payload, extra=extra,
            )
        ok, exc = await _resilience.with_retry(_attempt, peer_id=peer.server_id)
        _metrics.bump_peer(
            peer.server_id,
            forwards_attempted=1,
            bytes_out=approx_bytes,
            ok_responses=1 if ok else 0,
            error_responses=0 if ok else 1,
        )
        if not ok and exc is not None:
            logger.warning(
                "federation_forward_fail",
                peer=peer.server_id, error=str(exc),
            )
        return 1 if ok else 0

    results = await _asyncio.gather(
        *(_send(p) for p in targets), return_exceptions=True,
    )
    attempted = sum(1 for r in results if isinstance(r, int) and r > 0)
    _metrics.record_event(
        "forward_sent",
        target_user_id=target_user_id,
        event=event,
        message_id=message_id,
        hop_count=hop_count,
        peers=[p.server_id for p in targets],
        attempted=attempted,
    )
    logger.info(
        "federation_forwarded",
        target_user_id=target_user_id,
        event=event,
        message_id=message_id,
        hop_count=hop_count,
        peers_attempted=attempted,
        peers_excluded=len(exclude),
    )
    # Best-effort: push to admin socket room so dashboards see bridge
    # activity live. Runs fire-and-forget; if no admin is connected the
    # emit is a no-op.
    try:
        import asyncio as _asyncio
        from app.socket.server import sio as _sio
        _asyncio.create_task(_sio.emit(
            "admin:federation_event",
            {
                "kind": "forward_sent",
                "target_user_id": target_user_id,
                "event": event,
                "message_id": message_id,
                "hop_count": hop_count,
                "peers_attempted": attempted,
            },
            room="admin_federation",
        ))
    except Exception:
        pass
    return attempted
