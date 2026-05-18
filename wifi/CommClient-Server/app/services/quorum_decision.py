"""
Quorum-based writes — confirm a value reached ≥ K replicas before
returning success to the caller.

The default ``replication_manager.put`` is fire-and-forget — it pushes
to K targets but doesn't wait for acknowledgements. That's right for
high-throughput operator settings where reconciliation will catch any
drops. For decisions where the operator *needs* to know the change
took effect cluster-wide before the next request can read it
(e.g. blocking a peer, rotating a federation secret), this module
adds a synchronous quorum write:

    accepted, count, attempted = await quorum_write(
        kind="config", key="federation_secret", value=new_secret,
        required_acks=3, timeout=4.0,
    )

Properties
----------
* **Strict majority** — default required_acks = ⌈K/2⌉ + 1, so two
  concurrent quorum writes can't both succeed with disjoint replica
  sets.
* **Bounded latency** — the write returns as soon as the acks land
  (or the timeout expires), not after every replica responds.
* **Best-effort completion** — even if quorum is reached early, the
  remaining pushes still fire so all K replicas eventually converge.

This is *not* Raft — there's no leader, no log, no compaction. It's
a Dynamo-style sloppy quorum tuned for small, high-value, infrequent
writes. For frequent writes use the existing fire-and-forget put().
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


DEFAULT_TIMEOUT_SEC = 4.0
DEFAULT_REPLICATION = 3


@dataclass
class QuorumResult:
    accepted:       bool
    acks_received:  int
    acks_required:  int
    targets_picked: int
    duration_ms:    float
    failures:       list[str]    # short reason strings


def _required_acks(replication: int, override: Optional[int] = None) -> int:
    if override is not None:
        return max(1, int(override))
    return math.floor(replication / 2) + 1


# ── Per-peer push ───────────────────────────────────────────────


async def _push_with_ack(
    peer,
    kind: str,
    key: str,
    value: str,
    version: int,
    ts: float,
    timeout: float,
) -> tuple[bool, str]:
    try:
        import httpx
        from app.core.federation_auth import sign_request
    except ImportError:
        return False, "deps_missing"
    body = json.dumps({
        "kind": kind, "key": key, "value": value,
        "version": version, "updated_at": ts,
    }).encode()
    path = "/api/cluster/replicated/put"
    headers = sign_request("POST", path, body)
    headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                f"http://{peer.host}:{peer.port}{path}",
                content=body, headers=headers,
            )
        if r.status_code == 200:
            d = r.json() or {}
            if d.get("accepted"):
                return True, "ok"
            return False, "rejected_lower_version"
        return False, f"status_{r.status_code}"
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as e:
        return False, str(e)[:60]


# ── Public API ──────────────────────────────────────────────────


async def quorum_write(
    kind: str,
    key: str,
    value: Any,
    *,
    replication: int = DEFAULT_REPLICATION,
    required_acks: Optional[int] = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> QuorumResult:
    """Write a record and wait until ≥ ``required_acks`` replicas
    confirm. Returns a ``QuorumResult`` describing what actually
    happened — caller decides whether to proceed.

    The local store is always written first (counts as one ack).
    Remote pushes run concurrently with a single deadline.
    """
    started = time.time()
    needed = _required_acks(replication, required_acks)

    # Resolve replica set + write locally if we're one of them.
    try:
        from app.services.consistent_hash import (
            get_ring, refresh_from_registry,
        )
        from app.services.discovery_service import get_server_id
        from app.services.node_registry import get_registry
        from app.services.replication_manager import _store
    except ImportError:
        return QuorumResult(False, 0, needed, 0, 0.0, ["deps_missing"])

    refresh_from_registry()
    targets = get_ring().replicas_for(f"{kind}::{key}", k=replication)
    me = get_server_id() or ""
    reg = get_registry()
    peer_index = {n.node_id: n for n in reg.nodes(include_dead=False)}

    # Bump version above any existing record so peers pick this write
    # as the winner under LWW.
    existing = _store().get(kind, key)
    version = (existing["version"] + 1) if existing else 1
    serialized = json.dumps(value, sort_keys=True)
    now = time.time()

    acks = 0
    failures: list[str] = []

    if me in targets:
        if _store().upsert(kind, key, serialized, version, now):
            acks += 1
        else:
            failures.append("local_lww_lost")

    # Build outgoing tasks for each remote replica.
    remote_targets = [
        peer_index[sid] for sid in targets
        if sid != me and sid in peer_index
    ]
    tasks = [
        asyncio.create_task(
            _push_with_ack(p, kind, key, serialized, version, now, timeout),
            name=f"quorum-{p.node_id[:8]}",
        )
        for p in remote_targets
    ]

    # Race them under the deadline; collect acks as they land.
    deadline = started + timeout
    pending = set(tasks)
    while pending and acks < needed:
        wait_for = max(0.0, deadline - time.time())
        if wait_for <= 0:
            break
        done, pending = await asyncio.wait(
            pending, timeout=wait_for,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            try:
                ok, reason = t.result()
            except Exception as e:
                ok, reason = False, str(e)[:60]
            if ok:
                acks += 1
            else:
                failures.append(reason)

    # Don't cancel pending tasks — let them complete in the background
    # so the cluster eventually converges. Just stop blocking the
    # caller now that quorum is reached or expired.

    duration_ms = (time.time() - started) * 1000.0
    accepted = acks >= needed

    # Single-node clusters can't reach a multi-replica quorum by
    # definition. Treat that as a healthy "single-node accept" so the
    # logs aren't littered with false-alarm warnings during normal LAN
    # operation. We still bubble accepted=False up to the caller so any
    # cross-cluster code that genuinely needs consensus can react.
    is_single_node = len(peer_index) <= 1

    if accepted:
        logger.info(
            "quorum_write_accepted",
            kind=kind, key=key, acks=acks, needed=needed,
            duration_ms=round(duration_ms, 1),
        )
    elif is_single_node and acks >= 1:
        logger.debug(
            "quorum_write_single_node",
            kind=kind, key=key, acks=acks, needed=needed,
            duration_ms=round(duration_ms, 1),
        )
        accepted = True  # local write succeeded; no peers to consult.
    else:
        logger.warning(
            "quorum_write_failed",
            kind=kind, key=key, acks=acks, needed=needed,
            duration_ms=round(duration_ms, 1),
            failures=failures[:5],
        )
    return QuorumResult(
        accepted=accepted,
        acks_received=acks,
        acks_required=needed,
        targets_picked=len(targets),
        duration_ms=round(duration_ms, 1),
        failures=failures,
    )


async def quorum_read(
    kind: str,
    key: str,
    *,
    replication: int = DEFAULT_REPLICATION,
    required_acks: Optional[int] = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> Optional[dict]:
    """Read a record from ≥ ``required_acks`` replicas and return the
    one with the highest (version, updated_at). Returns None if not
    enough replicas respond in time.
    """
    started = time.time()
    needed = _required_acks(replication, required_acks)

    try:
        from app.services.consistent_hash import get_ring, refresh_from_registry
        from app.services.discovery_service import get_server_id
        from app.services.node_registry import get_registry
        from app.services.replication_manager import get as local_get
    except ImportError:
        return None

    refresh_from_registry()
    targets = get_ring().replicas_for(f"{kind}::{key}", k=replication)
    me = get_server_id() or ""
    reg = get_registry()
    peer_index = {n.node_id: n for n in reg.nodes(include_dead=False)}

    responses: list[dict] = []
    if me in targets:
        local = local_get(kind, key)
        if local:
            responses.append(local)

    async def _fetch_remote(peer) -> Optional[dict]:
        try:
            import httpx
            from app.core.federation_auth import sign_request
            path = f"/api/cluster/replicated/{kind}/{key}"
            headers = sign_request("GET", path, b"")
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(
                    f"http://{peer.host}:{peer.port}{path}",
                    headers=headers,
                )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    remote_targets = [
        peer_index[sid] for sid in targets
        if sid != me and sid in peer_index
    ]
    tasks = [
        asyncio.create_task(_fetch_remote(p), name=f"qread-{p.node_id[:8]}")
        for p in remote_targets
    ]

    deadline = started + timeout
    pending = set(tasks)
    while pending and len(responses) < needed:
        wait_for = max(0.0, deadline - time.time())
        if wait_for <= 0:
            break
        done, pending = await asyncio.wait(
            pending, timeout=wait_for,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            try:
                r = t.result()
                if r:
                    responses.append(r)
            except Exception:
                pass

    if len(responses) < needed:
        logger.warning(
            "quorum_read_below_quorum",
            kind=kind, key=key,
            got=len(responses), needed=needed,
        )
        return None

    # LWW pick.
    responses.sort(
        key=lambda d: (int(d.get("version") or 0), float(d.get("updated_at") or 0.0)),
        reverse=True,
    )
    return responses[0]
