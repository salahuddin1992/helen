"""
Broadcast coalescer — batch high-frequency Socket.IO events.

Why
---
On a 500-person call, every voice-activity update fires
``call:active_speaker`` (~3-5 Hz). A naive ``sio.emit(room=...)``
acquires the room lock, walks the membership set, and serialises
the payload N times — 500 emits per Hz => 2.5k+ broadcasts/sec for
ONE call. Multiply by concurrent calls and the asyncio loop chokes.

This coalescer:
  * Groups events by ``(call_id, event_name)`` key.
  * Within a flush window (default 100ms), keeps only the LATEST
    payload — older ones are superseded.
  * Flushes via a single ``asyncio.gather`` so the room emits run
    concurrently across calls.

Effect
------
A 500-person call going from "12 active-speaker emits/s × 500
recipients = 6,000 broadcasts/s" down to "10 emits/s × 500 = 5,000
broadcasts/s deduped to 1 emit per cycle". With multiple concurrent
calls the savings compound super-linearly.

Designed for events where stale data is acceptable (active speaker,
participant state flags, quality reports). Lifecycle events (join,
leave, hangup) MUST stay on the direct emit path.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# Type for the underlying emitter — usually ``sio.emit``.
EmitFn = Callable[..., Awaitable[None]]


@dataclass
class _Pending:
    event: str          # actual wire event name
    payload: dict
    room: str
    enqueued_at: float


class BroadcastCoalescer:
    """Per-key latest-payload coalescer with periodic flush.

    Keys are ``(call_id, event_name)`` tuples; payloads coming in
    against the same key replace the previous one. The flush task
    walks the pending map every ``flush_interval_sec`` and fires
    every queued emit concurrently.
    """

    def __init__(
        self,
        emit: EmitFn,
        flush_interval_sec: float = 0.1,
    ) -> None:
        self._emit = emit
        self._flush_interval = flush_interval_sec
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._lock = asyncio.Lock()
        self._flushed_total = 0
        self._coalesced_total = 0

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
        # Drain whatever is left so we don't lose a final speaker
        # change at shutdown.
        await self._flush_once()

    async def submit(
        self,
        call_id: str,
        event: str,
        payload: dict,
        room: Optional[str] = None,
        coalesce_key: Optional[str] = None,
    ) -> None:
        """Queue an emit. Replaces any pending entry for the same
        ``(call_id, coalesce_key or event)`` key. ``room`` defaults
        to ``call:{call_id}``.

        ``coalesce_key`` exists so callers can dedupe per-sub-key
        without changing the wire event name. Example: per-user
        participant-state flips coalesce on key=``state:{user_id}``
        but all emit as the canonical ``call_participant_state``.
        """
        key = (call_id, coalesce_key or event)
        target_room = room or f"call:{call_id}"
        async with self._lock:
            if key in self._pending:
                self._coalesced_total += 1
            self._pending[key] = _Pending(
                event=event,
                payload=payload,
                room=target_room,
                enqueued_at=time.time(),
            )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "pending": len(self._pending),
            "flushed_total": self._flushed_total,
            "coalesced_total": self._coalesced_total,
            "flush_interval_sec": self._flush_interval,
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
                try:
                    await self._flush_once()
                except Exception as exc:
                    logger.warning(
                        "broadcast_coalescer_flush_failed",
                        error=str(exc),
                    )
        except asyncio.CancelledError:
            return

    async def _flush_once(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            batch = list(self._pending.items())
            self._pending.clear()

        # Fire every emit concurrently — Socket.IO's room emit is
        # async and acquires the room set; gathering them lets the
        # loop interleave instead of serialising O(rooms) lock waits.
        coros = [
            self._emit(p.event, p.payload, room=p.room)
            for _key, p in batch
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        self._flushed_total += len(batch)
        for r in results:
            if isinstance(r, Exception):
                logger.warning(
                    "broadcast_coalescer_emit_failed",
                    error=str(r),
                )


# ── Module-level singleton ────────────────────────────────────────

_INSTANCE: Optional[BroadcastCoalescer] = None


def get_broadcast_coalescer() -> Optional[BroadcastCoalescer]:
    return _INSTANCE


def configure(emit: EmitFn, flush_interval_sec: float = 0.1) -> BroadcastCoalescer:
    global _INSTANCE
    _INSTANCE = BroadcastCoalescer(
        emit=emit, flush_interval_sec=flush_interval_sec,
    )
    return _INSTANCE
