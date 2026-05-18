"""
Federation client — issues signed HTTP calls to sibling Helen servers.

When a user on server A asks for someone by share_code and the code isn't
local, we fan out the query to every peer in `peer_registry`. Likewise,
when we need to emit a socket event to a user whose home server is B, we
POST it to B's `/api/federation/emit` and B re-emits locally.

All requests are HMAC-signed with `FEDERATION_SECRET` (see
`app.core.federation_auth`). Calls to peers that don't share the same
secret will come back 401/403 and are silently skipped.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.federation_auth import (
    HEADER_ORIGIN,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    sign_request,
)
from app.core.logging import get_logger
from app.services.discovery_service import get_server_id
from app.services.peer_registry import PeerRecord, peer_registry

logger = get_logger(__name__)
settings = get_settings()


@dataclass
class RemoteUser:
    """Minimal projection of a user hosted on another Helen server."""
    id: str
    username: str
    share_code: str
    display_name: str
    avatar_url: str | None
    status: str
    origin_server_id: str
    origin_host: str
    origin_port: int
    origin_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "share_code": self.share_code,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "status": self.status,
            "origin_server": {
                "server_id": self.origin_server_id,
                "name": self.origin_name,
                "host": self.origin_host,
                "port": self.origin_port,
            },
        }


class _PeerBreaker:
    """Per-peer circuit breaker. Three states:

      closed   — normal operation; failures counted.
      open     — failing fast; every request short-circuited until cooldown.
      half_open — one probe request allowed; success → closed, fail → open.
    """
    __slots__ = ("state", "fail_count", "opened_at", "peer")

    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.state = "closed"
        self.fail_count = 0
        self.opened_at = 0.0

    def _settings(self):
        from app.core.config import get_settings
        return get_settings()

    def allow(self) -> bool:
        s = self._settings()
        if self.state == "closed":
            return True
        if self.state == "open":
            if (time.time() - self.opened_at) >= s.FEDERATION_BREAKER_OPEN_SECONDS:
                self.state = "half_open"
                return True
            return False
        # half_open: at most one probe concurrently — we don't strictly
        # enforce that here; the check is best-effort, overshoot is
        # harmless (two probes = two chances to re-close).
        return True

    def record_success(self) -> None:
        if self.state != "closed":
            logger.info("federation_breaker_closed", peer=self.peer)
        self.state = "closed"
        self.fail_count = 0

    def record_failure(self) -> None:
        s = self._settings()
        self.fail_count += 1
        if self.state == "half_open":
            # Probe failed — re-open with a fresh cooldown.
            self.state = "open"
            self.opened_at = time.time()
            logger.info("federation_breaker_reopened", peer=self.peer)
            return
        if self.state == "closed" and self.fail_count >= s.FEDERATION_BREAKER_FAIL_THRESHOLD:
            self.state = "open"
            self.opened_at = time.time()
            logger.warning(
                "federation_breaker_opened",
                peer=self.peer, fail_count=self.fail_count,
            )


_breakers: dict[str, _PeerBreaker] = {}


def _breaker_for(peer_id: str) -> _PeerBreaker:
    b = _breakers.get(peer_id)
    if b is None:
        b = _PeerBreaker(peer_id)
        _breakers[peer_id] = b
    return b


def breaker_snapshot() -> list[dict[str, Any]]:
    """Exposed for /federation/metrics dashboards."""
    now = time.time()
    out = []
    for b in _breakers.values():
        out.append({
            "peer": b.peer,
            "state": b.state,
            "fail_count": b.fail_count,
            "open_age_seconds": (
                round(now - b.opened_at, 2) if b.opened_at else 0
            ),
        })
    return out


class FederationService:
    """Issues signed requests to peers. Single shared httpx.AsyncClient."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # share_code → (server_id, expires_at) to avoid re-fanning out for
        # recently-resolved codes. 10-minute TTL.
        self._lookup_cache: dict[str, tuple[str, float]] = {}
        self._cache_ttl = 600.0

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Tuned for high-fanout DHT traffic at 1M-server scale.
            # Default httpx limits (max_connections=100) become a
            # serialization point when the gossip worker, DHT
            # store/lookup, and chain forwarder all fire in the same
            # tick. Bumping these is cheap memory-wise (one TCP slot
            # each) and unblocks parallelism that the asyncio
            # scheduler can otherwise actually use.
            import os as _os_h
            try:
                max_conn = int(_os_h.environ.get(
                    "HELEN_FEDERATION_HTTP_MAX_CONNS", "1024"))
            except ValueError:
                max_conn = 1024
            try:
                max_keep = int(_os_h.environ.get(
                    "HELEN_FEDERATION_HTTP_MAX_KEEPALIVE", "256"))
            except ValueError:
                max_keep = 256
            self._client = httpx.AsyncClient(
                timeout=settings.FEDERATION_PEER_TIMEOUT_SECONDS,
                limits=httpx.Limits(
                    max_connections=max_conn,
                    max_keepalive_connections=max_keep,
                    keepalive_expiry=60.0,
                ),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _enabled(self) -> bool:
        return settings.FEDERATION_ENABLED and bool(settings.FEDERATION_SECRET)

    def _peer_url(self, peer: PeerRecord, path: str) -> str:
        scheme = "https" if peer.protocol == "https" else "http"
        return f"{scheme}://{peer.host}:{peer.port}{path}"

    async def _signed_request(
        self,
        peer: PeerRecord,
        method: str,
        path: str,
        json_body: dict | None = None,
    ) -> httpx.Response | None:
        body_bytes = (
            json.dumps(json_body, separators=(",", ":")).encode()
            if json_body is not None else b""
        )
        breaker = _breaker_for(peer.server_id)
        if not breaker.allow():
            logger.debug(
                "federation_breaker_short_circuit",
                peer=peer.server_id, state=breaker.state,
            )
            return None
        try:
            headers = sign_request(method, path, body_bytes)
        except RuntimeError:
            return None
        headers[HEADER_ORIGIN] = get_server_id()
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        url = self._peer_url(peer, path)
        try:
            client = self._get_client()
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers)
            else:
                resp = await client.request(
                    method, url, content=body_bytes, headers=headers,
                )
        except httpx.HTTPError as e:
            breaker.record_failure()
            logger.debug(
                "federation_peer_unreachable",
                peer=peer.server_id, host=peer.host, port=peer.port, error=str(e),
            )
            return None

        # Treat 5xx and auth rejections as breaker-tripping failures.
        # 4xx other than 401/403 are application-level and don't count
        # against peer health (e.g. 404 on a lookup miss is normal).
        if resp.status_code >= 500 or resp.status_code in (401, 403):
            breaker.record_failure()
        else:
            breaker.record_success()
        return resp

    # ── Public API ──────────────────────────────────────────

    async def lookup_user_by_code(self, code: str) -> RemoteUser | None:
        """Fan out to every live peer and return the first user match.

        Peers are queried in parallel; we bail out as soon as one responds
        with a 200. Cached for FEDERATION_PEER_TIMEOUT * 1 on hit.
        """
        if not self._enabled():
            return None

        # Fresh cache short-circuit
        entry = self._lookup_cache.get(code)
        if entry is not None and entry[1] > time.time():
            pass  # still need to call the peer to get the full user row

        peers = await peer_registry.list(include_stale=False)
        if not peers:
            return None

        path = f"/api/federation/users/by-code/{code}"
        tasks = [self._signed_request(p, "GET", path) for p in peers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for peer, resp in zip(peers, results):
            if not isinstance(resp, httpx.Response):
                continue
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except ValueError:
                continue
            user = data.get("user") or data
            if not user or not user.get("id"):
                continue
            remote = RemoteUser(
                id=user["id"],
                username=user.get("username", ""),
                share_code=user.get("share_code", code),
                display_name=user.get("display_name", ""),
                avatar_url=user.get("avatar_url"),
                status=user.get("status", "offline"),
                origin_server_id=peer.server_id,
                origin_host=peer.host,
                origin_port=peer.port,
                origin_name=peer.name,
            )
            self._lookup_cache[code] = (peer.server_id, time.time() + self._cache_ttl)
            # Seed the federated-emit cache so subsequent emits skip fan-out.
            from app.services.federated_emit import remember_origin
            remember_origin(remote.id, peer.server_id)
            # Also seed the federated_presence cache. Without this, the
            # immediate next call (e.g. v2_call_initiate against the looked-up
            # user) fails with "Target user not found or inactive" because
            # the federated_presence push is asynchronous and may lag behind
            # the share-code lookup that just resolved them. The lookup
            # itself is authoritative — it just confirmed the user lives
            # on this peer — so registering them as online is correct.
            try:
                from app.services.federated_presence import federated_presence
                await federated_presence.upsert(
                    user_id=remote.id,
                    username=remote.username,
                    display_name=remote.display_name,
                    origin_server_id=peer.server_id,
                    status=remote.status or "online",
                )
            except Exception:  # pragma: no cover — defensive
                pass
            return remote
        return None

    async def dht_store_user(
        self,
        peer: PeerRecord,
        *,
        user_id: str,
        origin_server_id: str,
        ttl_seconds: float = 120.0,
    ) -> bool:
        """Tell ``peer`` "user_id lives on origin_server_id". Used by the
        STORE half of Kademlia: when a user comes online here, we
        announce it to the K nearest peers so future lookups for that
        user resolve in O(log N) hops without flooding."""
        if not self._enabled():
            return False
        body = {
            "user_id": user_id,
            "origin_server_id": origin_server_id,
            "ttl_seconds": ttl_seconds,
        }
        resp = await self._signed_request(
            peer, "POST", "/api/federation/dht/store_user", json_body=body,
        )
        return resp is not None and resp.status_code == 202

    async def dht_find_user(
        self,
        peer: PeerRecord,
        *,
        user_id: str,
        k: int = 20,
    ) -> dict | None:
        """FIND_VALUE for a user — peer either knows the owner directly
        or returns its K-closest peers for further iteration. Returns
        None on transport failure."""
        if not self._enabled():
            return None
        body = {"user_id": user_id, "k": k}
        resp = await self._signed_request(
            peer, "POST", "/api/federation/dht/find_user", json_body=body,
        )
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    async def route_learned_hint(
        self,
        peer: PeerRecord,
        *,
        target_user_id: str,
        origin_server_id: str,
    ) -> bool:
        """Tell ``peer`` that ``target_user_id`` currently lives on
        ``origin_server_id``. Used by chain-routing backpropagation: when
        a flood message finally lands at its destination, every hop along
        the way gets told which peer owns the user, so follow-up messages
        skip the flood and go direct (amplification O(N) → O(1))."""
        if not self._enabled():
            return False
        body = {
            "target_user_id": target_user_id,
            "origin_server_id": origin_server_id,
        }
        resp = await self._signed_request(
            peer, "POST", "/api/federation/route/learned", json_body=body,
        )
        return resp is not None and resp.status_code == 202

    async def gossip_peers_to(
        self,
        peer: PeerRecord,
        known_peers: list[PeerRecord],
    ) -> int:
        """POST our peer list to ``peer`` so it can learn about servers it
        may not have discovered directly.

        Returns the count the remote claims to have ingested (peers it
        didn't already know). 0 means the remote already saw all of
        them — also a success, just a no-op.
        """
        if not self._enabled():
            return 0
        # Ship only the minimum the remote needs to ingest. We mark them
        # all as "commclient-server" so the generic ingest path accepts.
        # Exclude ourselves (would be dedup'd remote-side anyway).
        body = {
            "peers": [
                {
                    "server_id": p.server_id,
                    "name": p.name,
                    "host": p.host,
                    "port": p.port,
                    "version": p.version,
                    "protocol": p.protocol,
                    "users_online": p.users_online,
                    "uptime": p.uptime,
                }
                for p in known_peers
                if p.server_id != peer.server_id
            ],
        }
        resp = await self._signed_request(
            peer, "POST", "/api/federation/gossip/peers", json_body=body,
        )
        if resp is None:
            return 0
        try:
            return int(resp.json().get("ingested", 0))
        except Exception:
            return 0

    async def emit_to_remote_user(
        self,
        origin_server_id: str,
        target_user_id: str,
        event: str,
        payload: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Forward a socket event to the peer that hosts `target_user_id`.

        ``extra`` carries chain-routing metadata (``message_id``,
        ``hop_count``, ``max_hops``) that the receiving peer uses to
        dedupe loops and cap transit depth. Legacy emits without ``extra``
        still work — the receiver treats missing fields as ``hop_count=0``
        with a freshly-minted message_id.

        Returns True if we reached the peer and it ACK'd the enqueue.
        On failure the event lands in the DLQ for admin inspection.
        """
        if not self._enabled():
            return False
        peer = await peer_registry.get(origin_server_id)
        if peer is None or peer.is_stale:
            await self._record_federation_dlq(
                kind="federation_emit",
                reason="peer_missing_or_stale",
                payload={
                    "origin_server_id": origin_server_id,
                    "target_user_id": target_user_id,
                    "event": event,
                    "payload": payload,
                    "extra": extra or {},
                },
            )
            return False
        body: dict[str, Any] = {
            "target_user_id": target_user_id,
            "event": event,
            "payload": payload,
        }
        if extra:
            # Inline the chain-routing fields at the top of the body so
            # older receivers that don't know about them still succeed
            # (they simply ignore unknown keys).
            for k in ("message_id", "hop_count", "max_hops"):
                if k in extra:
                    body[k] = extra[k]
        resp = await self._signed_request(
            peer, "POST", "/api/federation/emit", json_body=body,
        )
        ok = resp is not None and resp.status_code == 202
        if not ok:
            status = getattr(resp, "status_code", None)
            await self._record_federation_dlq(
                kind="federation_emit",
                reason=f"http_{status}" if status else "no_response",
                payload={
                    "origin_server_id": origin_server_id,
                    "target_user_id": target_user_id,
                    "event": event,
                    "payload": payload,
                    "extra": extra or {},
                },
            )
        return ok

    async def forward_call_rpc_fanout(
        self,
        origin_server_ids: list[str],
        rpc: str,
        call_id: str,
        user_id: str,
        extra: dict[str, Any] | None = None,
        *,
        max_concurrency: int = 16,
    ) -> dict[str, dict[str, Any] | None]:
        """Forward the same RPC to multiple origins in PARALLEL.

        Used when a single action (e.g. ``end_for_everyone`` from a
        moderator) needs to fan out across every server that holds
        a participant of the call. Sequential forwarding scales
        O(servers) — for 50 federated servers that's 50× the latency.
        With this we cap concurrency at ``max_concurrency`` (default
        16, enough to saturate a typical 1Gbps LAN without thrashing
        the asyncio event loop).

        Returns ``{server_id: response_or_None}`` so the caller can
        log which peers actually applied the action.
        """
        if not origin_server_ids:
            return {}

        sem = asyncio.Semaphore(max_concurrency)

        async def _one(sid: str) -> tuple[str, dict[str, Any] | None]:
            async with sem:
                resp = await self.forward_call_rpc(
                    sid, rpc, call_id, user_id, extra=extra,
                )
                return sid, resp

        results = await asyncio.gather(
            *(_one(sid) for sid in origin_server_ids),
            return_exceptions=True,
        )
        out: dict[str, dict[str, Any] | None] = {}
        for r in results:
            if isinstance(r, BaseException):
                logger.warning(
                    "call_rpc_fanout_one_failed",
                    rpc=rpc, call_id=call_id, error=str(r),
                )
                continue
            sid, resp = r
            out[sid] = resp
        return out

    async def forward_call_rpc(
        self,
        origin_server_id: str,
        rpc: str,
        call_id: str,
        user_id: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Forward a call lifecycle RPC (accept/reject/leave/hangup/
        reinvite) to the server that owns the authoritative ActiveCall
        for ``call_id``.

        Used by the v2 lifecycle handlers when a callee lives on a
        sibling server: their local ``call_service.get_call`` returns
        None, but the authz shadow knows the origin — so we POST the
        action there and the origin runs ``call_service.<rpc>`` locally.

        Returns the parsed JSON response from the origin
        (``{"ok": True, "result": {...}}``) or ``None`` if the peer
        was unreachable / signed-request failed. Failures are written
        to the DLQ for admin inspection / replay.
        """
        if not self._enabled():
            return None
        peer = await peer_registry.get(origin_server_id)
        if peer is None or peer.is_stale:
            logger.warning("call_rpc_origin_peer_missing",
                           origin=origin_server_id, rpc=rpc, call_id=call_id)
            await self._record_federation_dlq(
                kind="federation_rpc",
                reason="origin_peer_missing_or_stale",
                payload={
                    "origin_server_id": origin_server_id,
                    "rpc": rpc, "call_id": call_id, "user_id": user_id,
                    "extra": extra or {},
                },
            )
            return None
        body = {
            "rpc": rpc,
            "call_id": call_id,
            "user_id": user_id,
            "extra": extra or {},
        }
        resp = await self._signed_request(
            peer, "POST", "/api/federation/call/rpc", json_body=body,
        )
        if resp is None or resp.status_code >= 400:
            status = getattr(resp, "status_code", None)
            logger.warning("call_rpc_forward_failed",
                           origin=origin_server_id, rpc=rpc, call_id=call_id,
                           status=status)
            await self._record_federation_dlq(
                kind="federation_rpc",
                reason=f"http_{status}" if status else "no_response",
                payload={
                    "origin_server_id": origin_server_id,
                    "rpc": rpc, "call_id": call_id, "user_id": user_id,
                    "extra": extra or {},
                },
            )
            return None
        try:
            return resp.json()
        except Exception as e:
            logger.warning("call_rpc_response_parse_failed", error=str(e))
            return None

    @staticmethod
    async def _record_federation_dlq(
        *, kind: str, reason: str, payload: dict[str, Any],
        error: str | None = None,
    ) -> None:
        """Best-effort DLQ recording. Imported lazily so a circular-
        import doesn't bite at module load time."""
        try:
            from app.services import dead_letter_service as _dls
            await _dls.record(
                kind=kind,
                reason=reason,
                error=error,
                payload=payload,
            )
        except Exception as e:
            logger.warning("federation_dlq_record_failed", error=str(e))


federation_service = FederationService()
