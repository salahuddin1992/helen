"""
Distributed presence service — Redis pub/sub real-time presence.

Replaces ``federated_presence`` polling (60s sync, 120s stale window)
with sub-second propagation via Redis pub/sub. Routing decisions —
"which server hosts user X?" — must be answered with fresh data,
otherwise the system routes events to a server the user just left.

Storage layout
--------------
    KEY  helen:presence:user:{user_id}            VALUE {server_id}
                                                  TTL   90 seconds (renewed every 30s)
    KEY  helen:presence:server:{server_id}:users  SET   of user_ids on this server
                                                  TTL   120 seconds
    CHAN helen:presence:changes                   PUB   {action, user_id, server_id, ts}

API
---
    >>> svc = DistributedPresenceService(redis_client, this_server_id="server_001")
    >>> await svc.set_online("user_abc")
    >>> await svc.heartbeat_loop_start("user_abc")  # background renewal
    >>> server = await svc.get_server_for("user_xyz")
    >>> async for change in svc.subscribe_changes():
    ...     print(change)

Fallback
--------
If ``redis_client`` is None, we degrade to in-process — ``set_online``
records into a local dict and ``get_server_for`` always returns the
local server_id (since we don't know about any others). This is
correct for single-server deployments.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Optional, Callable, Awaitable

from app.core.logging import get_logger

logger = get_logger(__name__)

# Tunables — chosen so a missed renewal still has 2× the heartbeat
# interval to recover before the entry becomes stale.
HEARTBEAT_TTL_SEC      = 90
HEARTBEAT_INTERVAL_SEC = 30
SERVER_INDEX_TTL_SEC   = 120


class DistributedPresenceService:
    def __init__(
        self,
        redis_client=None,
        this_server_id: str = "local",
    ) -> None:
        self._redis = redis_client
        self._this_server_id = this_server_id
        # In-process fallback. Maps user_id → (server_id, expires_at).
        self._local: dict[str, tuple[str, float]] = {}
        # Background heartbeat tasks per user_id.
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}
        self._stopped = asyncio.Event()

    @property
    def is_distributed(self) -> bool:
        return self._redis is not None

    # ── Read API ────────────────────────────────────────────────

    async def get_server_for(self, user_id: str) -> Optional[str]:
        """Return the server_id currently hosting ``user_id``, or
        ``None`` if the user is not online anywhere we can see."""
        if self._redis is not None:
            try:
                v = await self._redis.get(f"helen:presence:user:{user_id}")
                if v is None:
                    return None
                return v.decode("utf-8") if isinstance(v, bytes) else v
            except Exception as e:
                logger.warning("presence_lookup_failed", user_id=user_id, error=str(e))
                return None

        # In-process fallback.
        rec = self._local.get(user_id)
        if rec is None:
            return None
        server_id, expires_at = rec
        if time.time() > expires_at:
            self._local.pop(user_id, None)
            return None
        return server_id

    async def get_users_on(self, server_id: str) -> list[str]:
        """Return all users currently hosted by ``server_id``. Useful
        when a server dies and we need to evict its sessions."""
        if self._redis is not None:
            try:
                members = await self._redis.smembers(f"helen:presence:server:{server_id}:users")
                return [m.decode("utf-8") if isinstance(m, bytes) else m for m in members]
            except Exception as e:
                logger.warning("presence_server_index_failed", server_id=server_id, error=str(e))
                return []

        # In-process fallback — only useful for our own server.
        if server_id != self._this_server_id:
            return []
        now = time.time()
        return [
            uid for uid, (sid, exp) in self._local.items()
            if sid == server_id and exp > now
        ]

    # ── Write API ───────────────────────────────────────────────

    async def set_online(self, user_id: str) -> None:
        """Record that ``user_id`` is online on this server. Publishes
        an "online" change to the pub/sub channel."""
        await self._set_online_internal(user_id, publish=True)

    async def _set_online_internal(self, user_id: str, *, publish: bool) -> None:
        if self._redis is not None:
            try:
                async with self._redis.pipeline(transaction=False) as p:
                    p.setex(
                        f"helen:presence:user:{user_id}",
                        HEARTBEAT_TTL_SEC,
                        self._this_server_id,
                    )
                    p.sadd(
                        f"helen:presence:server:{self._this_server_id}:users",
                        user_id,
                    )
                    p.expire(
                        f"helen:presence:server:{self._this_server_id}:users",
                        SERVER_INDEX_TTL_SEC,
                    )
                    if publish:
                        p.publish(
                            "helen:presence:changes",
                            json.dumps({
                                "action": "online",
                                "user_id": user_id,
                                "server_id": self._this_server_id,
                                "ts": time.time(),
                            }),
                        )
                    await p.execute()
                return
            except Exception as e:
                logger.warning("presence_set_online_failed", user_id=user_id, error=str(e))
                # Fall through to in-process record so we don't silently
                # forget about this user.

        self._local[user_id] = (
            self._this_server_id,
            time.time() + HEARTBEAT_TTL_SEC,
        )

    async def set_offline(self, user_id: str) -> None:
        """Record that ``user_id`` has gone offline. Publishes the
        change so other servers can update their caches immediately."""
        if self._redis is not None:
            try:
                async with self._redis.pipeline(transaction=False) as p:
                    p.delete(f"helen:presence:user:{user_id}")
                    p.srem(
                        f"helen:presence:server:{self._this_server_id}:users",
                        user_id,
                    )
                    p.publish(
                        "helen:presence:changes",
                        json.dumps({
                            "action": "offline",
                            "user_id": user_id,
                            "server_id": self._this_server_id,
                            "ts": time.time(),
                        }),
                    )
                    await p.execute()
            except Exception as e:
                logger.warning("presence_set_offline_failed", user_id=user_id, error=str(e))

        self._local.pop(user_id, None)
        # Stop heartbeat task if running.
        task = self._heartbeat_tasks.pop(user_id, None)
        if task is not None and not task.done():
            task.cancel()

    # ── Heartbeat lifecycle ─────────────────────────────────────

    async def heartbeat_loop_start(self, user_id: str) -> None:
        """Start a background heartbeat task that renews this user's
        presence every HEARTBEAT_INTERVAL_SEC. Idempotent."""
        if user_id in self._heartbeat_tasks:
            return
        task = asyncio.create_task(self._heartbeat_loop(user_id))
        self._heartbeat_tasks[user_id] = task

    async def _heartbeat_loop(self, user_id: str) -> None:
        try:
            # Initial publish.
            await self._set_online_internal(user_id, publish=True)
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=HEARTBEAT_INTERVAL_SEC,
                    )
                    return  # stopped
                except asyncio.TimeoutError:
                    pass  # renewal tick
                # Renew without re-publishing — the TTL refresh is
                # silent. Subscribers don't need a flood of
                # "still-online" notifications every 30s.
                await self._set_online_internal(user_id, publish=False)
        except asyncio.CancelledError:
            return

    async def stop(self) -> None:
        """Stop all heartbeat tasks. Call on shutdown."""
        self._stopped.set()
        tasks = list(self._heartbeat_tasks.values())
        self._heartbeat_tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, BaseException):
                pass

    # ── Subscribe to remote changes ─────────────────────────────

    async def subscribe_changes(
        self,
        handler: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> AsyncIterator[dict]:
        """Subscribe to the presence-change pub/sub channel. If
        ``handler`` is provided, it's invoked for each change. Either
        way, this is an async generator that yields the change dicts
        — caller can `async for` it directly. Without Redis, yields
        nothing (single-server deployment doesn't need cross-server
        notifications)."""
        if self._redis is None:
            return
        try:
            async with self._redis.pubsub() as pubsub:
                await pubsub.subscribe("helen:presence:changes")
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        data = json.loads(msg["data"])
                    except Exception:
                        continue
                    # Skip our own changes — we already have the local state.
                    if data.get("server_id") == self._this_server_id:
                        continue
                    if handler is not None:
                        try:
                            await handler(data)
                        except Exception as e:
                            logger.warning("presence_handler_failed", error=str(e))
                    yield data
        except Exception as e:
            logger.warning("presence_subscribe_failed", error=str(e))


# ── Module-level singleton ──────────────────────────────────────────

_svc: Optional[DistributedPresenceService] = None


def get_presence_service() -> DistributedPresenceService:
    global _svc
    if _svc is None:
        _svc = DistributedPresenceService(redis_client=None)
    return _svc


def configure(redis_client, this_server_id: str) -> DistributedPresenceService:
    """Install a Redis-backed presence service as the module singleton.
    Call from app/main.py after redis_client is connected and the
    server_id is known."""
    global _svc
    _svc = DistributedPresenceService(
        redis_client=redis_client,
        this_server_id=this_server_id,
    )
    logger.info(
        "distributed_presence_service_configured",
        mode="redis" if redis_client is not None else "in-process",
        server_id=this_server_id,
    )
    return _svc
