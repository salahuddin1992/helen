"""
Idempotency cache — keyed by (call_id, idempotency_key).

Used to deduplicate state-changing call events:
  • call:accept
  • call:decline
  • call:join

Without this, a network blip mid-emit (or the user double-tapping
Accept) can land two of the same event, racing into create-two-
participants or accept-then-decline windows.

Strategy
--------
- In-memory dict (single-server only — the cache is process-local).
  For the multi-server cluster case, swap with Redis SETNX + TTL —
  same API.
- TTL 5 min — long enough to absorb double-clicks and slow networks,
  short enough that a 24-hour-old replay can't poison.
- Bounded eviction: if entries >10k, drop entries older than TTL.

API
---
    >>> cache = IdempotencyCache()
    >>> result = await cache.get_or_compute(
    ...     call_id, idempotency_key,
    ...     factory=lambda: do_the_work(),
    ... )
The factory runs at most once per (call_id, key); subsequent
invocations within the TTL return the cached value.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TTL_SECONDS = 300
EVICTION_THRESHOLD  = 10_000


class IdempotencyCache:
    def __init__(self) -> None:
        # (call_id, key) → (result, inserted_at_monotonic)
        self._cache: dict[tuple[str, str], tuple[Any, float]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        # Per-key inflight Future — prevents two simultaneous callers
        # from both running the factory (would still be deduplicated by
        # the cache hit on the second call, but the factory might race).
        self._inflight: dict[tuple[str, str], asyncio.Future[Any]] = {}

    async def get_or_compute(
        self,
        call_id: str,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> Any:
        cache_key = (call_id, key)
        now = time.monotonic()
        is_leader = False

        # Fast-path: cache hit
        async with self._lock:
            entry = self._cache.get(cache_key)
            if entry and now - entry[1] < ttl_seconds:
                logger.info("idempotency_hit", call_id=call_id, key=key)
                return entry[0]
            # Inflight already?
            if cache_key in self._inflight:
                fut = self._inflight[cache_key]
            else:
                fut = asyncio.get_event_loop().create_future()
                self._inflight[cache_key] = fut
                is_leader = True
                # Eviction sweep
                if len(self._cache) > EVICTION_THRESHOLD:
                    self._evict_stale(now, ttl_seconds)

        if not is_leader:
            # We were a follower — wait for the leader's result.
            return await fut

        # We're the leader — run the factory, populate cache, resolve future.
        # Use try/except/finally so an unexpected error (e.g. cancellation)
        # never leaves an orphan inflight entry blocking future requests.
        try:
            result = await factory()
        except BaseException as exc:
            async with self._lock:
                self._inflight.pop(cache_key, None)
            if not fut.done():
                fut.set_exception(exc)
            raise

        async with self._lock:
            self._cache[cache_key] = (result, now)
            self._inflight.pop(cache_key, None)
        if not fut.done():
            fut.set_result(result)
        return result

    def _evict_stale(self, now: float, ttl: int) -> None:
        """Caller holds self._lock."""
        before = len(self._cache)
        self._cache = {
            k: v for k, v in self._cache.items()
            if now - v[1] < ttl
        }
        evicted = before - len(self._cache)
        if evicted:
            logger.info("idempotency_evicted_stale", count=evicted)

    def stats(self) -> dict:
        return {
            'size': len(self._cache),
            'inflight': len(self._inflight),
        }


# Module-level singleton — one cache per process.
idempotency = IdempotencyCache()
