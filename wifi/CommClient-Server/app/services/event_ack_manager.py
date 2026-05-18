"""
Event ACK manager — tracks ``requires_ack=True`` envelopes, schedules
retry on timeout, and DLQs after ``max_retries`` exhausted.

Why
---
Today, when ``federation_service.forward_call_rpc`` returns ``None``
(peer unreachable), the caller logs a warning and moves on. The DLQ
wiring we added earlier records the failure for inspection, but
nobody actually retries it. The result: a transient peer outage =
permanent event loss for any P0/P1 envelope.

This module owns the retry lifecycle. Workflow:
::

    # Producer side
    env = Envelope.new(..., priority="P0", requires_ack=True)
    await ack_manager.track(env, send_fn=publish_to_broker)
    # send_fn is called immediately. The manager keeps the envelope
    # until either (a) a matching ACK arrives, or (b) ttl_ms elapses.

    # ACK side (when a downstream hop ACKs)
    await ack_manager.record_ack(env.event_id)

    # On timeout
    if retry_count < max_retries:
        env = env.with_retry()
        await send_fn(env)   # exponential backoff
    else:
        await dlq.record(kind="event_ack_timeout", payload=env.dict())

The send_fn callable is supplied per-track call so the same manager
serves broker_client, federation_service, and the chain executor with
no awareness of which transport ACKs them.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app.core.logging import get_logger
from app.services.event_envelope import Envelope

logger = get_logger(__name__)

SendFn = Callable[[Envelope], Awaitable[bool]]
"""Async function that ships an envelope. Returns True on accepted-by-
transport (e.g. broker 202), False otherwise. The manager retries on
False AND on missed ACK."""


@dataclass
class _PendingAck:
    envelope: Envelope
    send_fn: SendFn
    enqueued_at: float
    ack_received: asyncio.Event = field(default_factory=asyncio.Event)
    retry_task: Optional[asyncio.Task] = None


class EventAckManager:
    """Process-local ACK tracker. Adequate for single-server tests
    and for the producer-side of inter-server retry. Distributed
    coordination of an "in-flight" set across servers isn't needed —
    the same producer that emitted the event also retries it."""

    def __init__(self, *, dlq_recorder: Optional[Callable] = None) -> None:
        self._pending: dict[str, _PendingAck] = {}
        self._dlq_recorder = dlq_recorder
        self._stopped = asyncio.Event()
        self._metrics = {
            "tracked": 0,
            "acked": 0,
            "retried": 0,
            "dlq_after_retries": 0,
            "expired_no_ack": 0,
        }

    # ── Public API ─────────────────────────────────────────────

    async def track(self, env: Envelope, send_fn: SendFn) -> bool:
        """Send an envelope and track for ACK. Caller awaits send;
        retry is scheduled in background. Returns True if the initial
        send succeeded; False if the very first send failed (still
        retried, but caller may want to reflect failure to user)."""
        if env.event_id in self._pending:
            logger.warning("ack_already_tracking", event_id=env.event_id)
            return True
        if not env.requires_ack:
            # Fire-and-forget — no tracking.
            try:
                return await send_fn(env)
            except Exception as e:
                logger.warning("send_fn_threw", event_id=env.event_id, error=str(e))
                return False

        pending = _PendingAck(
            envelope=env,
            send_fn=send_fn,
            enqueued_at=time.time(),
        )
        self._pending[env.event_id] = pending
        self._metrics["tracked"] += 1

        first_ok = False
        try:
            first_ok = await send_fn(env)
        except Exception as e:
            logger.warning("ack_send_threw", event_id=env.event_id, error=str(e))
            first_ok = False

        # Schedule the retry/timeout watcher whether or not first send
        # succeeded — the watcher waits for ACK either way.
        pending.retry_task = asyncio.create_task(self._retry_loop(pending))
        return first_ok

    async def record_ack(self, event_id: str) -> bool:
        """Mark ``event_id`` as ACK'd. Returns True if we were
        tracking it. Idempotent."""
        pending = self._pending.pop(event_id, None)
        if pending is None:
            return False
        pending.ack_received.set()
        if pending.retry_task is not None:
            pending.retry_task.cancel()
        self._metrics["acked"] += 1
        return True

    def is_tracking(self, event_id: str) -> bool:
        return event_id in self._pending

    def metrics(self) -> dict[str, int]:
        return {**self._metrics, "in_flight": len(self._pending)}

    async def stop(self) -> None:
        self._stopped.set()
        for pending in list(self._pending.values()):
            if pending.retry_task is not None:
                pending.retry_task.cancel()
        self._pending.clear()

    # ── Internal ───────────────────────────────────────────────

    async def _retry_loop(self, pending: _PendingAck) -> None:
        """Wait for ACK or per-attempt deadline. On miss, retry with
        exponential backoff while we still have TTL budget. After
        max_retries OR overall TTL exhaustion, DLQ.

        We split the envelope's total ``ttl_ms`` into ``max_retries+1``
        attempt windows so retries actually fit inside the budget.
        Without this, the first attempt waits the entire TTL and the
        very next check trips ``is_expired()`` and we never retry."""
        try:
            env = pending.envelope
            initial = pending.envelope
            # Per-attempt timeout — leaves headroom for retries within
            # the overall ttl_ms budget. P0 ttl=5000ms, retries=3 →
            # ~1.25s per attempt.
            attempts_total = max(1, env.max_retries + 1)
            attempt_timeout_sec = (env.ttl_ms / 1000.0) / attempts_total
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(
                        pending.ack_received.wait(),
                        timeout=attempt_timeout_sec,
                    )
                    return  # ACK arrived
                except asyncio.TimeoutError:
                    pass

                # Drop if expired beyond practical retry window.
                if env.is_expired():
                    self._metrics["expired_no_ack"] += 1
                    self._pending.pop(env.event_id, None)
                    return

                # Retry budget?
                if env.retry_count >= env.max_retries:
                    self._metrics["dlq_after_retries"] += 1
                    self._pending.pop(env.event_id, None)
                    if self._dlq_recorder is not None:
                        try:
                            await self._dlq_recorder(env, reason="ack_timeout_max_retries")
                        except Exception as e:
                            logger.warning("ack_dlq_failed", error=str(e))
                    logger.warning(
                        "ack_max_retries_exceeded",
                        event_id=env.event_id,
                        event_type=env.event_type,
                        retries=env.retry_count,
                    )
                    return

                # Exponential backoff: 100ms, 300ms, 700ms, 1500ms ...
                # capped at 10s. Adds modest jitter so retry storms
                # don't synchronize across many in-flight events.
                import random
                base = 0.1 * (2 ** env.retry_count)
                backoff = min(10.0, base) * (1.0 + random.uniform(-0.2, 0.2))
                await asyncio.sleep(backoff)

                # Build a retry envelope with incremented retry_count.
                # Replace the tracked envelope so subsequent ACK
                # matching uses the new event_id (the old span_id is
                # parented by the retry — easy to follow in traces).
                retry_env = env.with_retry()
                self._pending.pop(env.event_id, None)
                self._pending[retry_env.event_id] = pending
                pending.envelope = retry_env
                env = retry_env
                self._metrics["retried"] += 1
                logger.info(
                    "ack_retry_attempt",
                    event_id=env.event_id,
                    parent_event_id=env.parent_span_id,
                    retry_count=env.retry_count,
                )
                try:
                    await pending.send_fn(env)
                except Exception as e:
                    logger.warning("ack_retry_send_failed",
                                   event_id=env.event_id, error=str(e))
        except asyncio.CancelledError:
            return


# ── Module-level singleton ──────────────────────────────────────────

_svc: Optional[EventAckManager] = None


def get_ack_manager() -> EventAckManager:
    global _svc
    if _svc is None:
        _svc = EventAckManager()
    return _svc


def configure(*, dlq_recorder=None) -> EventAckManager:
    global _svc
    _svc = EventAckManager(dlq_recorder=dlq_recorder)
    return _svc
