"""
Edge — distributed read cache.

Stores frequently-read entities (user profile, channel metadata, file
URLs, presence). Backed by Redis when available, in-memory LRU
otherwise. Cluster invalidations flow over ``cluster.pubsub``.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


DEFAULT_TTL = 60.0
DEFAULT_MAX = 50_000
INVALIDATION_CHANNEL = "edge:invalidate"


class _LRU:
    """Bounded in-memory LRU with TTL."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._data: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            v = self._data.get(key)
            if v is None:
                return None
            value, exp = v
            if exp < time.monotonic():
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    async def set(self, key: str, value: Any, ttl: float) -> None:
        async with self._lock:
            self._data[key] = (value, time.monotonic() + ttl)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def invalidate_prefix(self, prefix: str) -> int:
        async with self._lock:
            to_remove = [k for k in self._data if k.startswith(prefix)]
            for k in to_remove:
                self._data.pop(k, None)
            return len(to_remove)

    def size(self) -> int:
        return len(self._data)


class EdgeCache:
    """Two-tier cache. Local LRU + (optional) Redis."""

    def __init__(self, *, maxsize: int = DEFAULT_MAX) -> None:
        self._lru = _LRU(maxsize)
        self._redis: Any = None
        self._sub_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        # Best-effort attach to redis if cluster session store has it.
        try:
            from app.services.cluster.session_store import (
                RedisSessionStore, get_session_store,
            )
            store = await get_session_store()
            if isinstance(store, RedisSessionStore):
                self._redis = store._redis  # type: ignore[attr-defined]
                self._sub_task = asyncio.create_task(
                    self._sub_loop(), name="edge-cache-sub",
                )
        except Exception as exc:
            logger.debug("edge_cache_redis_unavailable err=%s", exc)
        # Subscribe to cluster pubsub for invalidations.
        try:
            from app.services.cluster.pubsub import get_pubsub
            pub = get_pubsub()
            pub.subscribe(INVALIDATION_CHANNEL, self._on_invalidate)
        except Exception:
            pass

    async def stop(self) -> None:
        if self._sub_task is not None:
            self._sub_task.cancel()
            self._sub_task = None

    async def get(self, key: str) -> Optional[Any]:
        v = await self._lru.get(key)
        if v is not None:
            return v
        if self._redis is not None:
            try:
                raw = await self._redis.get(f"edge:cache:{key}")
                if raw:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="ignore")
                    return json.loads(raw)
            except Exception:
                pass
        return None

    async def set(
        self, key: str, value: Any, *, ttl: float = DEFAULT_TTL,
    ) -> None:
        await self._lru.set(key, value, ttl)
        if self._redis is not None:
            try:
                await self._redis.set(
                    f"edge:cache:{key}",
                    json.dumps(value, default=str),
                    ex=int(ttl),
                )
            except Exception:
                pass

    async def invalidate(self, key: str) -> None:
        await self._lru.invalidate(key)
        if self._redis is not None:
            try:
                await self._redis.delete(f"edge:cache:{key}")
            except Exception:
                pass
        await self._broadcast({"op": "invalidate", "key": key})

    async def invalidate_prefix(self, prefix: str) -> None:
        await self._lru.invalidate_prefix(prefix)
        # Skip Redis full-scan; rely on pubsub fanout.
        await self._broadcast({"op": "invalidate_prefix", "prefix": prefix})

    # ── pubsub ──────────────────────────────────────────────

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        try:
            from app.services.cluster.pubsub import get_pubsub
            await get_pubsub().publish(INVALIDATION_CHANNEL, payload)
        except Exception:
            pass

    async def _on_invalidate(self, _channel: str, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        if op == "invalidate":
            k = payload.get("key")
            if k:
                await self._lru.invalidate(k)
        elif op == "invalidate_prefix":
            p = payload.get("prefix")
            if p:
                await self._lru.invalidate_prefix(p)

    async def _sub_loop(self) -> None:
        # Redis-backed parallel listener kept lightweight — main pubsub
        # is the cluster bus. This is just keep-alive for the Redis client.
        while True:
            await asyncio.sleep(30)
            try:
                if self._redis is not None:
                    await self._redis.ping()
            except Exception:
                return

    def size(self) -> int:
        return self._lru.size()


_cache: Optional[EdgeCache] = None


def get_edge_cache() -> EdgeCache:
    global _cache
    if _cache is None:
        _cache = EdgeCache()
    return _cache
