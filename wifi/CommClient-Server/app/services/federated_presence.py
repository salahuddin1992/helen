"""
Cross-server presence cache.

Each Helen server keeps a best-effort view of which users are online on
peer servers. The view is built from two signals:

  1. Push: when a user connects or disconnects locally, we fan out a
     signed POST to every live peer so they can stamp `last_seen` in
     their own cache. No ACK required — presence is advisory, not
     load-bearing.
  2. Pull: on startup and every `_RESYNC_INTERVAL`, we call every peer's
     `/federation/presence/snapshot` to rebuild the cache from scratch
     in case we missed pushes during a partition.

The cache is in-memory, keyed by `user_id`. Entries expire after
`_PRESENCE_TTL` with no refresh so a peer dropping off the network
eventually disappears from everyone's view.

Consumed by:
  * `/api/federation/presence/directory`  — admin / search-ahead lookups
  * federated_emit routing shortcuts      — already have origin cache,
    but this one is richer (includes display_name + last_seen)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_PRESENCE_TTL = 120.0  # seconds before a remote presence entry expires
_RESYNC_INTERVAL = float(__import__("os").environ.get(
    "HELEN_FEDERATION_PRESENCE_RESYNC_SECONDS", "60",
))


@dataclass
class RemotePresence:
    user_id: str
    username: str
    display_name: str
    origin_server_id: str
    status: str = "online"       # online / away / busy / offline
    last_seen: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_seen) > _PRESENCE_TTL

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "origin_server_id": self.origin_server_id,
            "status": self.status,
            "last_seen": self.last_seen,
            "age_seconds": round(time.time() - self.last_seen, 2),
        }


class FederatedPresenceCache:
    """In-memory presence index, plus background resync loop.

    Distributed-presence integration (Phase 1)
    -----------------------------------------
    The stale window used to be ``_PRESENCE_TTL`` (120s) — a user
    going offline could remain visible for two minutes. We now also
    listen to the routing-level ``distributed_presence_service``
    Redis pub/sub channel and reactively drop entries on "offline"
    events. Online events still arrive via the rich HTTP push because
    routing events only carry user_id + server_id (no display_name).
    Net effect: offline detection drops from <=120s to <1s p99
    without changing the read API for callers.
    """

    def __init__(self) -> None:
        self._entries: dict[str, RemotePresence] = {}
        self._lock = asyncio.Lock()
        self._resync_task: asyncio.Task | None = None
        self._distributed_listener_task: asyncio.Task | None = None

    # ── Read API ────────────────────────────────────────────

    async def list_online(self) -> list[dict[str, Any]]:
        async with self._lock:
            fresh = [e for e in self._entries.values() if not e.is_expired]
        return [e.to_dict() for e in fresh]

    async def get(self, user_id: str) -> RemotePresence | None:
        async with self._lock:
            e = self._entries.get(user_id)
        if e is None or e.is_expired:
            return None
        return e

    # ── Write API (called from the HTTP endpoint) ───────────

    async def upsert(
        self,
        user_id: str,
        username: str,
        display_name: str,
        origin_server_id: str,
        status: str = "online",
    ) -> None:
        async with self._lock:
            self._entries[user_id] = RemotePresence(
                user_id=user_id,
                username=username,
                display_name=display_name,
                origin_server_id=origin_server_id,
                status=status,
                last_seen=time.time(),
            )

    async def remove(self, user_id: str) -> None:
        async with self._lock:
            self._entries.pop(user_id, None)

    async def reap_expired(self) -> int:
        async with self._lock:
            dead = [uid for uid, e in self._entries.items() if e.is_expired]
            for uid in dead:
                self._entries.pop(uid, None)
        return len(dead)

    # ── Push: advertise local changes to peers ──────────────

    async def broadcast_online(
        self,
        user_id: str,
        username: str,
        display_name: str,
    ) -> int:
        return await self._broadcast("online", user_id, username, display_name)

    async def broadcast_offline(self, user_id: str) -> int:
        return await self._broadcast("offline", user_id, "", "")

    async def _broadcast(
        self,
        kind: str,
        user_id: str,
        username: str,
        display_name: str,
    ) -> int:
        from app.core.config import get_settings
        settings = get_settings()
        if not settings.FEDERATION_ENABLED or not settings.FEDERATION_SECRET:
            return 0

        from app.services.discovery_service import get_server_id
        from app.services.federation_service import federation_service
        from app.services.peer_registry import peer_registry

        peers = await peer_registry.list(include_stale=False)
        if not peers:
            return 0
        payload = {
            "kind": kind,              # "online" | "offline"
            "user_id": user_id,
            "username": username,
            "display_name": display_name,
            "origin_server_id": get_server_id(),
        }

        async def _post(peer) -> bool:
            resp = await federation_service._signed_request(
                peer, "POST", "/api/federation/presence",
                json_body=payload,
            )
            return resp is not None and resp.status_code in (200, 202)

        results = await asyncio.gather(
            *[_post(p) for p in peers], return_exceptions=True,
        )
        ok = sum(1 for r in results if r is True)
        logger.info(
            "federated_presence_broadcast",
            kind=kind, user_id=user_id,
            peers_reached=ok, peers_total=len(peers),
        )
        try:
            from app.services.federation_metrics import incr
            incr("presence_pushes_sent", ok)
        except Exception:
            pass
        return ok

    # ── Pull: periodic resync ───────────────────────────────

    async def start_resync_loop(self) -> None:
        if self._resync_task is not None and not self._resync_task.done():
            return
        self._resync_task = asyncio.create_task(self._resync_loop())

    async def stop_resync_loop(self) -> None:
        if self._resync_task is not None:
            self._resync_task.cancel()
            try:
                await self._resync_task
            except (asyncio.CancelledError, Exception):
                pass
            self._resync_task = None
        if self._distributed_listener_task is not None:
            self._distributed_listener_task.cancel()
            try:
                await self._distributed_listener_task
            except (asyncio.CancelledError, Exception):
                pass
            self._distributed_listener_task = None

    # ── Distributed presence integration ───────────────────────

    async def start_distributed_listener(self) -> None:
        """Subscribe to ``distributed_presence_service`` pub/sub
        changes so we can reactively drop offline entries instead of
        waiting for the 120s TTL. No-op when no Redis is configured —
        the polling loop still provides eventual consistency."""
        if self._distributed_listener_task is not None and not self._distributed_listener_task.done():
            return
        try:
            from app.services.distributed_presence_service import get_presence_service
            svc = get_presence_service()
            if not svc.is_distributed:
                return  # No Redis — nothing to subscribe to
        except Exception as e:
            logger.warning("distributed_presence_unavailable", error=str(e))
            return
        self._distributed_listener_task = asyncio.create_task(
            self._distributed_listen_loop(),
        )

    async def _distributed_listen_loop(self) -> None:
        from app.services.distributed_presence_service import get_presence_service
        svc = get_presence_service()
        try:
            async for change in svc.subscribe_changes():
                action = change.get("action")
                user_id = change.get("user_id")
                if not user_id:
                    continue
                if action == "offline":
                    await self.remove(user_id)
                    logger.debug(
                        "federated_presence_reactive_offline",
                        user_id=user_id,
                    )
                # On "online" we DON'T upsert — the routing event
                # lacks the rich fields. The HTTP push handler will
                # populate the entry with display_name etc.
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("distributed_presence_listener_error", error=str(e))

    async def _resync_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_RESYNC_INTERVAL)
                await self._resync_once()
                await self.reap_expired()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("federated_presence_resync_error", error=str(e))

    async def _resync_once(self) -> None:
        from app.core.config import get_settings
        if not get_settings().FEDERATION_ENABLED:
            return
        from app.services.federation_service import federation_service
        from app.services.peer_registry import peer_registry

        peers = await peer_registry.list(include_stale=False)
        if not peers:
            return

        async def _fetch(peer):
            resp = await federation_service._signed_request(
                peer, "GET", "/api/federation/presence/snapshot",
            )
            if resp is None or resp.status_code != 200:
                return peer.server_id, []
            try:
                return peer.server_id, resp.json().get("online") or []
            except ValueError:
                return peer.server_id, []

        results = await asyncio.gather(
            *[_fetch(p) for p in peers], return_exceptions=True,
        )
        for r in results:
            if not isinstance(r, tuple):
                continue
            sid, users = r
            for u in users:
                if not u.get("user_id"):
                    continue
                await self.upsert(
                    user_id=u["user_id"],
                    username=u.get("username", ""),
                    display_name=u.get("display_name", ""),
                    origin_server_id=sid,
                    status=u.get("status", "online"),
                )


federated_presence = FederatedPresenceCache()
