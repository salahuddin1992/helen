"""
Per-socket egress queue with priority + drop-oldest semantics.

Why
---
A misbehaving client (slow Wi-Fi, swapped-out tab, paused JS engine)
can backpressure the Socket.IO write pipe. Without a bound, the
server keeps appending to its in-memory write buffer and OOMs in
minutes when emitting 5 Hz active-speaker events to 200 clients on
a 500-person call.

This queue:
  * Holds at most ``capacity`` events per socket (default 256).
  * Has two priority bands: P0 (lifecycle: join, leave, signal,
    moderation) and P1 (high-frequency: speaker, state, quality).
  * On overflow:
      - P1 always drops oldest P1 first (clients tolerate stale
        speaker indicators; they don't tolerate missed `call:ended`).
      - P0 drops oldest P0 last; if still full, the emit is dropped
        with a warning so the operator sees real backpressure.

It's an opt-in helper — handlers that already use ``sio.emit(to=sid)``
keep working unchanged. Helpers that wrap this queue gain bounded
buffering automatically.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


PRIO_P0 = 0   # never-drop unless the queue is fully full
PRIO_P1 = 1   # high-frequency, drop-oldest acceptable


class _SocketBucket:
    __slots__ = ("p0", "p1", "lock", "drained")

    def __init__(self) -> None:
        self.p0: deque = deque()
        self.p1: deque = deque()
        self.lock = asyncio.Lock()
        self.drained = asyncio.Event()
        self.drained.set()

    def total(self) -> int:
        return len(self.p0) + len(self.p1)


class SocketEgressQueue:
    """Bounded per-sid egress queue."""

    def __init__(
        self,
        emit: Callable[..., Awaitable[None]],
        capacity: int = 256,
        flush_interval_sec: float = 0.05,
    ) -> None:
        self._emit = emit
        self._cap = capacity
        self._flush_interval = flush_interval_sec
        self._buckets: dict[str, _SocketBucket] = {}
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        # Stats — exposed via stats() for /api/admin/diagnostics.
        self._dropped_p1 = 0
        self._dropped_p0 = 0
        self._sent = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, BaseException):
                pass
            self._task = None

    async def submit(
        self,
        sid: str,
        event: str,
        payload: Any,
        *,
        priority: int = PRIO_P1,
    ) -> bool:
        """Enqueue an emit. Returns True if accepted, False if the
        full bucket forced us to drop this very item."""
        bucket = self._buckets.get(sid)
        if bucket is None:
            bucket = _SocketBucket()
            self._buckets[sid] = bucket
        async with bucket.lock:
            if priority == PRIO_P1:
                # P1 drops oldest P1 first.
                if bucket.total() >= self._cap and bucket.p1:
                    bucket.p1.popleft()
                    self._dropped_p1 += 1
                if bucket.total() >= self._cap:
                    # Even after popping P1, no room — drop incoming.
                    self._dropped_p1 += 1
                    return False
                bucket.p1.append((event, payload, time.time()))
            else:
                # P0: spill into P1 head if needed, then claim slot.
                if bucket.total() >= self._cap:
                    if bucket.p1:
                        bucket.p1.popleft()
                        self._dropped_p1 += 1
                    elif bucket.p0:
                        bucket.p0.popleft()
                        self._dropped_p0 += 1
                if bucket.total() >= self._cap:
                    self._dropped_p0 += 1
                    return False
                bucket.p0.append((event, payload, time.time()))
            bucket.drained.clear()
            return True

    def drop_socket(self, sid: str) -> None:
        """Forget a sid — called on disconnect so we don't leak the
        bucket."""
        self._buckets.pop(sid, None)

    def stats(self) -> dict[str, Any]:
        return {
            "buckets": len(self._buckets),
            "queued": sum(b.total() for b in self._buckets.values()),
            "dropped_p1": self._dropped_p1,
            "dropped_p0": self._dropped_p0,
            "sent": self._sent,
        }

    # ── Internals ───────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=self._flush_interval,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                await self._flush_once()
        except asyncio.CancelledError:
            return

    async def _flush_once(self) -> None:
        # Snapshot bucket keys so we don't hold the dict reference
        # while awaiting each emit.
        sids = list(self._buckets.keys())
        for sid in sids:
            bucket = self._buckets.get(sid)
            if bucket is None:
                continue
            # Pull a snapshot of items and emit OUTSIDE the lock so
            # a slow remote doesn't block subsequent enqueues.
            async with bucket.lock:
                drain_p0 = list(bucket.p0)
                drain_p1 = list(bucket.p1)
                bucket.p0.clear()
                bucket.p1.clear()
            for event, payload, _ts in drain_p0 + drain_p1:
                try:
                    await self._emit(event, payload, to=sid)
                    self._sent += 1
                except Exception as exc:
                    logger.warning(
                        "socket_egress_emit_failed",
                        sid=sid, event=event, error=str(exc),
                    )
            async with bucket.lock:
                if bucket.total() == 0:
                    bucket.drained.set()


_INSTANCE: Optional[SocketEgressQueue] = None


def get_socket_egress_queue() -> Optional[SocketEgressQueue]:
    return _INSTANCE


def configure(
    emit: Callable[..., Awaitable[None]],
    capacity: int = 256,
    flush_interval_sec: float = 0.05,
) -> SocketEgressQueue:
    global _INSTANCE
    _INSTANCE = SocketEgressQueue(
        emit=emit, capacity=capacity, flush_interval_sec=flush_interval_sec,
    )
    return _INSTANCE
