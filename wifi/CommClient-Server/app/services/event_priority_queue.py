"""
Priority queue router for distributed events.

Five queues, one per priority class (P0–P4), each with its own:
  * size cap
  * overflow policy (drop oldest / drop new / DLQ / evict-from-lower)
  * retry semantics (delegated to event_ack_manager)
  * tracing level

Why
---
Today every server-to-server event flows through the same code path.
A typing-indicator burst can starve call signaling because they share
the asyncio event loop unfairly. This module gives each priority its
own queue and consumer, so P0 (call signal) is never blocked behind
P3 (presence flood).

API
---
    >>> q = PriorityRouter(broker_client)
    >>> await q.publish(envelope)            # routes to correct queue
    >>> async for env in q.consume("P0"):   # one consumer per priority
    ...     await handle(env)
    >>> q.depth("P0")                        # current queue size
    >>> q.metrics()                          # full snapshot

Backpressure / overflow
-----------------------
* P0 overflow → evict oldest P3 to make room (P0 is critical).
* P1/P2 overflow → DLQ the new event (don't drop existing in-flight).
* P3 overflow → drop oldest (typing/presence is best-effort).
* P4 overflow → drop oldest.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from app.core.logging import get_logger
from app.services.event_envelope import Envelope

logger = get_logger(__name__)

# Per-priority size caps. Tuned for a single server with ~1000
# concurrent users; scale linearly with capacity.
DEFAULT_CAPS = {
    "P0": 500,
    "P1": 1000,
    "P2": 5000,
    "P3": 2000,   # presence — best-effort, drop oldest
    "P4": 5000,   # file metadata — best-effort
}

# Overflow policy keyed by priority.
OVERFLOW_POLICY = {
    "P0": "evict_lower",  # critical: evict from P3 if needed
    "P1": "dlq",
    "P2": "dlq",
    "P3": "drop_oldest",
    "P4": "drop_oldest",
}


@dataclass
class _QueueMetrics:
    published: int = 0
    consumed: int = 0
    dropped_overflow: int = 0
    dropped_expired: int = 0
    dropped_dlq: int = 0


class PriorityRouter:
    def __init__(
        self,
        *,
        caps: Optional[dict[str, int]] = None,
        dlq_recorder=None,
    ) -> None:
        caps = caps or DEFAULT_CAPS
        self._queues: dict[str, asyncio.Queue[Envelope]] = {
            p: asyncio.Queue(maxsize=caps[p]) for p in DEFAULT_CAPS
        }
        self._caps = caps
        self._metrics: dict[str, _QueueMetrics] = {p: _QueueMetrics() for p in DEFAULT_CAPS}
        self._dlq_recorder = dlq_recorder
        self._lock = asyncio.Lock()

    # ── Publish ────────────────────────────────────────────────

    async def publish(self, env: Envelope) -> bool:
        """Enqueue an envelope. Returns True on accepted, False on
        dropped. Idempotency / dedup is the caller's responsibility —
        this is purely a queue."""
        if env.is_expired():
            self._metrics[env.priority].dropped_expired += 1
            return False

        q = self._queues.get(env.priority)
        if q is None:
            logger.warning("unknown_priority", priority=env.priority,
                           event_id=env.event_id)
            return False

        try:
            q.put_nowait(env)
            self._metrics[env.priority].published += 1
            return True
        except asyncio.QueueFull:
            return await self._handle_overflow(env)

    async def _handle_overflow(self, env: Envelope) -> bool:
        policy = OVERFLOW_POLICY.get(env.priority, "dlq")

        if policy == "drop_oldest":
            # Drop oldest in this queue, enqueue the new one.
            async with self._lock:
                q = self._queues[env.priority]
                try:
                    _ = q.get_nowait()
                    q.task_done()
                    self._metrics[env.priority].dropped_overflow += 1
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(env)
                    self._metrics[env.priority].published += 1
                    return True
                except asyncio.QueueFull:
                    # Race — another publisher took the slot. Give up.
                    self._metrics[env.priority].dropped_overflow += 1
                    return False

        if policy == "evict_lower":
            # P0 own-queue overflow: keep the newest, evict the oldest
            # from the SAME queue. Older P0 is more likely to be stale
            # (offer that's already been retransmitted, ICE candidate
            # the remote already learned about). Newer P0 has fresher
            # state. We also opportunistically free space in lower
            # queues so the system as a whole shows pressure relief.
            async with self._lock:
                q = self._queues[env.priority]
                try:
                    _ = q.get_nowait()
                    q.task_done()
                    self._metrics[env.priority].dropped_overflow += 1
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(env)
                    self._metrics[env.priority].published += 1
                except asyncio.QueueFull:
                    # Race — could not free space in our own queue.
                    # Fall through to DLQ.
                    logger.error(
                        "priority_p0_dlq",
                        event_id=env.event_id,
                        event_type=env.event_type,
                    )
                    await self._dlq(env, reason="p0_own_queue_overflow_race")
                    return False
            # Best-effort relief in lower queues — drop one P3 and one
            # P4 to keep aggregate pressure visible to downstream load
            # tracking. Caller already has its slot in P0.
            for victim_pri in ("P3", "P4"):
                victim_q = self._queues[victim_pri]
                try:
                    _ = victim_q.get_nowait()
                    victim_q.task_done()
                    self._metrics[victim_pri].dropped_overflow += 1
                except asyncio.QueueEmpty:
                    pass
            return True

        # Default policy: DLQ
        await self._dlq(env, reason=f"{env.priority}_overflow")
        return False

    async def _dlq(self, env: Envelope, reason: str) -> None:
        self._metrics[env.priority].dropped_dlq += 1
        if self._dlq_recorder is None:
            return
        try:
            await self._dlq_recorder(env, reason)
        except Exception as e:
            logger.warning("dlq_recorder_failed", error=str(e))

    # ── Consume ────────────────────────────────────────────────

    async def consume(self, priority: str) -> AsyncIterator[Envelope]:
        """Async iterator that yields envelopes from the priority's
        queue. Caller's loop body should be cheap; long-running work
        should hand off to a worker pool."""
        q = self._queues.get(priority)
        if q is None:
            return
        while True:
            env = await q.get()
            try:
                # Expiry check at consume time too — events can age
                # while sitting in queue.
                if env.is_expired():
                    self._metrics[priority].dropped_expired += 1
                    continue
                self._metrics[priority].consumed += 1
                yield env
            finally:
                q.task_done()

    def depth(self, priority: str) -> int:
        q = self._queues.get(priority)
        return q.qsize() if q is not None else 0

    def all_depths(self) -> dict[str, int]:
        return {p: q.qsize() for p, q in self._queues.items()}

    def metrics(self) -> dict[str, dict]:
        out = {}
        for p, m in self._metrics.items():
            out[p] = {
                "depth": self.depth(p),
                "cap": self._caps[p],
                "published": m.published,
                "consumed": m.consumed,
                "dropped_overflow": m.dropped_overflow,
                "dropped_expired": m.dropped_expired,
                "dropped_dlq": m.dropped_dlq,
            }
        return out


# ── Module-level singleton ──────────────────────────────────────────

_router: Optional[PriorityRouter] = None


def get_router() -> PriorityRouter:
    global _router
    if _router is None:
        _router = PriorityRouter()
    return _router


def configure(*, caps: Optional[dict[str, int]] = None, dlq_recorder=None) -> PriorityRouter:
    global _router
    _router = PriorityRouter(caps=caps, dlq_recorder=dlq_recorder)
    return _router
