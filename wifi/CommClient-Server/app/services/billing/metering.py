"""
Usage metering with buffered batched writes.

`record_usage` is a hot path: every message send / file upload / token
consumed hits it. We don't want to commit to the DB on every event, so
this module keeps an in-memory accumulator and flushes every
``FLUSH_INTERVAL_SEC`` (default 60s) or when the buffer crosses
``MAX_BUFFER`` rows.

Aggregation is by (workspace_id, metric, period_start, period_end).
"""
from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.billing import UsageRecord, VALID_USAGE_METRICS

logger = get_logger(__name__)


FLUSH_INTERVAL_SEC = 60
MAX_BUFFER = 5_000


# ───────────────────────────────────────────────────────────────────────
# Buffer
# ───────────────────────────────────────────────────────────────────────


@dataclass
class _BufKey:
    workspace_id: str
    metric: str
    period_start: datetime
    period_end: datetime

    def __hash__(self) -> int:
        return hash((self.workspace_id, self.metric,
                     self.period_start.timestamp(),
                     self.period_end.timestamp()))


@dataclass
class _BufValue:
    total: float = 0.0
    last_source: str = "system"
    metadata: dict[str, Any] = field(default_factory=dict)


class _Meter:
    """Thread-safe in-memory accumulator with periodic async flushing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf: dict[_BufKey, _BufValue] = defaultdict(_BufValue)
        self._size = 0
        self._task: asyncio.Task | None = None
        self._started = False

    # ── Public API ────────────────────────────────────────────────
    def record(
        self,
        *,
        workspace_id: str,
        metric: str,
        value: float = 1.0,
        source: str = "system",
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not workspace_id or not metric:
            return
        if metric not in VALID_USAGE_METRICS:
            logger.debug("metering.unknown-metric %s", metric)
            return
        ps, pe = _period_for(period_start, period_end)
        key = _BufKey(workspace_id, metric, ps, pe)
        with self._lock:
            v = self._buf[key]
            v.total += float(value)
            v.last_source = source
            if metadata:
                v.metadata.update(metadata)
            self._size += 1
        if self._size >= MAX_BUFFER:
            asyncio.create_task(self.flush())   # noqa: RUF006

    async def flush(self) -> int:
        """Drain the buffer to the DB. Returns the row count flushed."""
        with self._lock:
            snapshot = list(self._buf.items())
            self._buf.clear()
            self._size = 0
        if not snapshot:
            return 0
        async with async_session_factory() as db:    # type: AsyncSession
            try:
                for key, val in snapshot:
                    existing = (await db.execute(
                        select(UsageRecord).where(and_(
                            UsageRecord.workspace_id == key.workspace_id,
                            UsageRecord.metric == key.metric,
                            UsageRecord.period_start == key.period_start,
                            UsageRecord.period_end == key.period_end,
                        ))
                    )).scalar_one_or_none()
                    if existing is None:
                        db.add(UsageRecord(
                            workspace_id=key.workspace_id,
                            metric=key.metric,
                            value=val.total,
                            recorded_at=datetime.now(timezone.utc),
                            period_start=key.period_start,
                            period_end=key.period_end,
                            source=val.last_source,
                            metadata_json=val.metadata,
                        ))
                    else:
                        existing.value = float(existing.value) + val.total
                        existing.recorded_at = datetime.now(timezone.utc)
                        existing.source = val.last_source
                        merged = dict(existing.metadata_json or {})
                        merged.update(val.metadata)
                        existing.metadata_json = merged
                await db.commit()
            except Exception as e:                                          # noqa: BLE001
                logger.error("metering.flush failed: %s", e)
                await db.rollback()
                return 0
        logger.debug("metering.flush wrote=%s", len(snapshot))
        return len(snapshot)

    # ── Background loop ──────────────────────────────────────────
    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(FLUSH_INTERVAL_SEC)
                await self.flush()
            except asyncio.CancelledError:
                await self.flush()
                raise
            except Exception as e:                                          # noqa: BLE001
                logger.error("metering.loop error: %s", e)

    def start(self) -> None:
        if self._started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("metering.start: no running loop")
            return
        self._task = loop.create_task(self._loop())
        self._started = True
        logger.info("metering.background.started interval=%ss", FLUSH_INTERVAL_SEC)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._started = False


_meter = _Meter()


# ───────────────────────────────────────────────────────────────────────
# Public functions
# ───────────────────────────────────────────────────────────────────────


def _period_for(
    period_start: datetime | None, period_end: datetime | None,
) -> tuple[datetime, datetime]:
    """Default to current calendar month."""
    if period_start and period_end:
        return period_start, period_end
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Next month
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def record_usage(
    workspace_id: str,
    metric: str,
    value: float = 1.0,
    source: str = "system",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Buffered usage tick. Safe to call from sync code."""
    _meter.record(
        workspace_id=workspace_id, metric=metric,
        value=value, source=source, metadata=metadata,
    )


async def aggregate_usage(
    db: AsyncSession,
    workspace_id: str,
    metric: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> dict[str, float]:
    """Return ``{metric: total}`` aggregated across all rows in the period."""
    ps, pe = _period_for(period_start, period_end)
    stmt = select(
        UsageRecord.metric, func.sum(UsageRecord.value),
    ).where(and_(
        UsageRecord.workspace_id == workspace_id,
        UsageRecord.period_start >= ps,
        UsageRecord.period_end <= pe,
    ))
    if metric:
        stmt = stmt.where(UsageRecord.metric == metric)
    stmt = stmt.group_by(UsageRecord.metric)
    rows = (await db.execute(stmt)).all()
    return {m: float(t or 0) for m, t in rows}


async def history(
    db: AsyncSession,
    workspace_id: str,
    metric: str,
    *,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Time-series of one metric, daily buckets."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(
            UsageRecord.recorded_at, UsageRecord.value,
        ).where(and_(
            UsageRecord.workspace_id == workspace_id,
            UsageRecord.metric == metric,
            UsageRecord.recorded_at >= cutoff,
        )).order_by(UsageRecord.recorded_at)
    )).all()
    return [
        {"t": r[0].isoformat() if r[0] else None, "v": float(r[1] or 0)}
        for r in rows
    ]


def start_background_flusher() -> None:
    _meter.start()


async def stop_background_flusher() -> None:
    await _meter.stop()


async def force_flush() -> int:
    return await _meter.flush()
