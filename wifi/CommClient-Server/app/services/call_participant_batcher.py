"""
Batched writer for call-participant persistence.

Under mass-join bursts (e.g. 500+ users entering a call within a few hundred
ms), firing one SQLite transaction per participant serializes through the
single-writer lock and starves the signaling event loop. This batcher
coalesces pending add/remove operations and flushes them in grouped
transactions — one lock acquisition per N items instead of N.

Durability is unchanged vs. the previous fire-and-forget path: participant
rows are still persisted under WAL + synchronous=NORMAL, and the authoritative
end-of-call record is written by ``persist_call_log`` on leave/hangup.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select, update

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.active_call import ActiveCallParticipant

logger = get_logger(__name__)


FLUSH_INTERVAL_SEC = 0.05   # flush at least this often (50ms)
FLUSH_BATCH_SIZE = 100      # flush early if queue reaches this size


@dataclass(slots=True)
class _Op:
    kind: Literal["add", "remove"]
    call_id: str
    user_id: str
    sid: str | None = None
    role: str = "participant"


class CallParticipantBatcher:
    def __init__(self) -> None:
        self._queue: list[_Op] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        self._flush_task = loop.create_task(self._run(), name="call_participant_batcher")

    async def stop(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._flush()  # drain before shutdown

    def enqueue_add(self, call_id: str, user_id: str, sid: str | None = None, role: str = "participant") -> None:
        self._queue.append(_Op("add", call_id, user_id, sid, role))
        if len(self._queue) >= FLUSH_BATCH_SIZE:
            self._wake.set()

    def enqueue_remove(self, call_id: str, user_id: str) -> None:
        self._queue.append(_Op("remove", call_id, user_id))
        if len(self._queue) >= FLUSH_BATCH_SIZE:
            self._wake.set()

    async def _run(self) -> None:
        while True:
            try:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=FLUSH_INTERVAL_SEC)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("participant_batcher_loop_error", error=str(exc))
                await asyncio.sleep(0.1)

    async def _flush(self) -> None:
        async with self._lock:
            if not self._queue:
                return
            ops = self._queue
            self._queue = []

        # Coalesce: for each (call_id, user_id) keep only the *latest* op.
        # This drops redundant writes when a user adds+removes quickly.
        latest: dict[tuple[str, str], _Op] = {}
        for op in ops:
            latest[(op.call_id, op.user_id)] = op

        adds = [o for o in latest.values() if o.kind == "add"]
        removes = [o for o in latest.values() if o.kind == "remove"]

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        try:
            async with async_session_factory() as s:
                # Fetch existing rows in ONE query instead of one per op.
                if adds:
                    existing = (await s.execute(
                        select(ActiveCallParticipant).where(
                            ActiveCallParticipant.call_id.in_({o.call_id for o in adds}),
                            ActiveCallParticipant.user_id.in_({o.user_id for o in adds}),
                        )
                    )).scalars().all()
                    existing_map = {(r.call_id, r.user_id): r for r in existing}

                    for op in adds:
                        row = existing_map.get((op.call_id, op.user_id))
                        if row is None:
                            s.add(ActiveCallParticipant(
                                call_id=op.call_id,
                                user_id=op.user_id,
                                sid=op.sid,
                                role=op.role,
                                joined_at=now,
                            ))
                        else:
                            row.sid = op.sid
                            row.role = op.role
                            row.left_at = None
                            row.joined_at = now

                # Bulk UPDATE for removes — one statement per (call_id, user_id).
                for op in removes:
                    await s.execute(
                        update(ActiveCallParticipant)
                        .where(
                            ActiveCallParticipant.call_id == op.call_id,
                            ActiveCallParticipant.user_id == op.user_id,
                            ActiveCallParticipant.left_at.is_(None),
                        )
                        .values(left_at=now)
                    )

                await s.commit()
        except Exception as exc:
            # Re-enqueue on failure so nothing is lost — bounded retry via natural loop.
            logger.error("participant_batch_flush_failed", size=len(latest), error=str(exc))
            async with self._lock:
                self._queue.extend(ops)


# Singleton
call_participant_batcher = CallParticipantBatcher()
