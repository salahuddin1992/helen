"""Priority queue for distributed-lock acquisitions.

The base ``distributed_lock`` is first-come-first-served. For locks
contended between admin actions and background tasks, we want admin
to skip the line. This module wraps the lock primitive with a
priority queue: higher numeric priority = serviced first.

Priority constants:

    PRIORITY_ADMIN      = 100
    PRIORITY_OPERATOR   = 50
    PRIORITY_BACKGROUND = 10

Internal: each lock name has its own waiter queue. A worker drains
the queue in priority order, attempting acquire one at a time. When
acquired, the waiter's coroutine is woken with the lease.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator


PRIORITY_ADMIN      = 100
PRIORITY_OPERATOR   = 50
PRIORITY_BACKGROUND = 10


@dataclass(order=True)
class _Waiter:
    # Lower tuple = higher priority. Uses negative priority so heap
    # gives us highest priority first.
    sort_key:    tuple = field(init=False, repr=False)
    priority:    int   = field(compare=False)
    enqueued_at: float = field(compare=False)
    waiter_id:   str   = field(compare=False)
    future:      asyncio.Future = field(compare=False)

    def __post_init__(self) -> None:
        self.sort_key = (-self.priority, self.enqueued_at)


class _PerLockQueue:
    def __init__(self, name: str) -> None:
        self.name = name
        self.lock = threading.RLock()
        self.waiters: list[_Waiter] = []  # heap-ordered
        self.holder_id: str | None = None

    def add(self, w: _Waiter) -> None:
        import heapq
        with self.lock:
            heapq.heappush(self.waiters, w)

    def pop_next(self) -> _Waiter | None:
        import heapq
        with self.lock:
            if not self.waiters:
                return None
            return heapq.heappop(self.waiters)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "name":          self.name,
                "holder_id":     self.holder_id,
                "queue_depth":   len(self.waiters),
                "next_priority": (
                    -self.waiters[0].sort_key[0]
                    if self.waiters else None
                ),
            }


class LockPriorityQueueRegistry:
    _singleton: "LockPriorityQueueRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._queues: dict[str, _PerLockQueue] = {}

    @classmethod
    def instance(cls) -> "LockPriorityQueueRegistry":
        if cls._singleton is None:
            cls._singleton = LockPriorityQueueRegistry()
        return cls._singleton

    def _get(self, name: str) -> _PerLockQueue:
        with self._lock:
            q = self._queues.get(name)
            if q is None:
                q = _PerLockQueue(name)
                self._queues[name] = q
            return q

    @asynccontextmanager
    async def acquire(self, name: str,
                      *, priority: int = PRIORITY_BACKGROUND,
                      ttl_sec: float = 300.0,
                      timeout: float = 60.0) -> AsyncIterator[bool]:
        """Wait in priority order, then acquire the underlying
        distributed_lock when our turn arrives. Yields True on
        success, False on timeout."""
        import uuid
        from app.services.distributed_lock import distributed_lock

        q = self._get(name)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        wid = uuid.uuid4().hex
        w = _Waiter(priority=priority, enqueued_at=time.time(),
                    waiter_id=wid, future=fut)
        q.add(w)

        # Single-threaded drain — only one waiter at a time tries to
        # actually acquire the underlying lock. The queue order is
        # the priority order.
        deadline = time.time() + timeout
        try:
            while True:
                next_w = q.pop_next()
                if next_w is None:
                    # Shouldn't happen since we just added ourselves;
                    # treat as timeout.
                    yield False
                    return
                if next_w.waiter_id != wid:
                    # Another waiter goes first — re-add and wait.
                    q.add(next_w)
                    if time.time() >= deadline:
                        yield False
                        return
                    await asyncio.sleep(0.05)
                    continue

                # Our turn — acquire underlying lock.
                async with distributed_lock(name, ttl=ttl_sec) as held:
                    q.holder_id = wid if held else None
                    try:
                        yield held
                    finally:
                        q.holder_id = None
                return
        except Exception:
            # Make sure we don't deadlock the queue if we crashed.
            raise

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "queues": [q.snapshot() for q in self._queues.values()],
            }


def get_lock_priority_queue() -> LockPriorityQueueRegistry:
    return LockPriorityQueueRegistry.instance()
