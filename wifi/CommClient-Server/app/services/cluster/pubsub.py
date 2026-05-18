"""
Phase 6 / Module AC — Cross-node pub/sub.

Two transport modes:

* **Redis pub/sub** — preferred when ``settings.REDIS_URL`` is set.
* **HTTP fan-out**   — fallback. Posts to every active peer's
                       ``/api/admin/cluster/pubsub/ingest`` endpoint.

Used by:

* socket.io message delivery to users connected on a remote node
* live admin UI broadcasts (security events, audit log tail, alerts)
* cluster-wide cache invalidation
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.cluster.leader_election import _resolve_node_id
from app.services.cluster.node_registry import get_node_registry
from app.services.cluster.session_store import (
    RedisSessionStore,
    SessionStore,
    get_session_store,
)

logger = get_logger(__name__)


Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


class PubSub:
    """Cluster pub/sub abstraction. ``publish`` is fire-and-forget; the
    receiving side calls registered handlers concurrently."""

    def __init__(self) -> None:
        self.node_id = _resolve_node_id()
        self._handlers: dict[str, list[Handler]] = {}
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._store: Optional[SessionStore] = None
        self._redis_pub_task: Optional[asyncio.Task[None]] = None
        self._http_recent: dict[str, float] = {}
        self._http_lock = asyncio.Lock()

    # ── lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._store = await get_session_store()
        self._stop.clear()
        if isinstance(self._store, RedisSessionStore):
            self._task = asyncio.create_task(self._redis_subscribe_loop(),
                                             name="pubsub-redis-subscribe")
            logger.info("pubsub: redis transport enabled")
        else:
            # HTTP fanout is reactive — nothing to start
            logger.info("pubsub: http fan-out transport enabled")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:                            # pragma: no cover
                self._task.cancel()

    # ── public API ──────────────────────────────────────────

    def subscribe(self, channel: str, handler: Handler) -> None:
        self._handlers.setdefault(channel, []).append(handler)

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        envelope = {
            "id": uuid.uuid4().hex,
            "channel": channel,
            "origin_node": self.node_id,
            "ts": time.time(),
            "payload": payload,
        }
        if isinstance(self._store, RedisSessionStore):
            try:
                r = self._store._redis  # type: ignore[attr-defined]
                await r.publish(f"helen:bus:{channel}", json.dumps(envelope, default=str))
                return
            except Exception as exc:                                # pragma: no cover
                logger.warning("pubsub: redis publish failed (%s), falling back to HTTP", exc)
        await self._http_fanout(envelope)

    async def ingest(self, envelope: dict[str, Any]) -> None:
        """Entry-point used by HTTP fan-out handlers."""
        env_id = envelope.get("id")
        if env_id is None:
            return
        # dedup by recent IDs
        async with self._http_lock:
            now = time.time()
            # prune old entries
            for k, ts in list(self._http_recent.items()):
                if now - ts > 60:
                    self._http_recent.pop(k, None)
            if env_id in self._http_recent:
                return
            self._http_recent[env_id] = now
        await self._dispatch(envelope)

    # ── internals ───────────────────────────────────────────

    async def _dispatch(self, envelope: dict[str, Any]) -> None:
        channel = envelope.get("channel") or ""
        payload = envelope.get("payload") or {}
        handlers = list(self._handlers.get(channel, []))
        if not handlers:
            return
        await asyncio.gather(
            *(self._safe(channel, payload, h) for h in handlers),
            return_exceptions=True,
        )

    async def _safe(self, channel: str, payload: dict[str, Any], h: Handler) -> None:
        try:
            await h(channel, payload)
        except Exception as exc:                                    # pragma: no cover
            logger.exception("pubsub: handler crashed on %s: %s", channel, exc)

    async def _redis_subscribe_loop(self) -> None:
        assert isinstance(self._store, RedisSessionStore)
        r = self._store._redis  # type: ignore[attr-defined]
        try:
            pubsub = r.pubsub()
            await pubsub.psubscribe("helen:bus:*")
            while not self._stop.is_set():
                try:
                    msg = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    continue
                if not msg:
                    continue
                try:
                    data = msg.get("data")
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    env = json.loads(data) if isinstance(data, str) else data
                    if env.get("origin_node") == self.node_id:
                        continue
                    await self._dispatch(env)
                except Exception as exc:                            # pragma: no cover
                    logger.exception("pubsub: redis dispatch err: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                    # pragma: no cover
            logger.exception("pubsub: redis loop crashed: %s", exc)

    async def _http_fanout(self, envelope: dict[str, Any]) -> None:
        try:
            import httpx
        except Exception:                                           # pragma: no cover
            logger.warning("pubsub: httpx missing, fan-out skipped")
            return
        nodes = await get_node_registry().get_active_nodes()
        if not nodes:
            return
        # Always run locally first
        await self.ingest(envelope)
        peers = [n for n in nodes if n.node_id != self.node_id]
        if not peers:
            return
        async with httpx.AsyncClient(timeout=2.5) as cli:
            await asyncio.gather(
                *(self._http_post_one(cli, n.advertise_url, envelope) for n in peers),
                return_exceptions=True,
            )

    async def _http_post_one(self, cli, base_url: str, envelope: dict[str, Any]) -> None:
        try:
            url = base_url.rstrip("/") + "/api/admin/cluster/pubsub/ingest"
            await cli.post(url, json=envelope)
        except Exception:                                           # pragma: no cover
            pass


# ── singleton ───────────────────────────────────────────────


_singleton: Optional[PubSub] = None


def get_pubsub() -> PubSub:
    global _singleton
    if _singleton is None:
        _singleton = PubSub()
    return _singleton
