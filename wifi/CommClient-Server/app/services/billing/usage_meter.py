"""
UsageMeter — admin-portal friendly wrapper around the existing buffered
``metering.record_usage`` pipeline.

The legacy metering module is optimised for fire-and-forget event
counters (messages, files, calls). For the Tenancy/Billing portal we
also need:

  * per-endpoint counters (``api.<METHOD>.<route>``)
  * per-user counters within a tenant
  * byte-in / byte-out accumulators
  * timeseries pulls keyed by (tenant, endpoint, period)

We layer this surface on top of the existing :mod:`metering` module —
``record`` translates each call into one or more ``record_usage`` ticks
so the existing background flusher persists them, AND we keep a
shorter-lived in-memory bucket indexed by ``(tenant, endpoint, user)``
that the admin endpoints can read without round-tripping the DB. The
in-memory buckets are pruned every hour.

Returned dicts are JSON-serialisable so the Portal can render them
directly.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.billing import UsageRecord, VALID_USAGE_METRICS
from app.services.billing import metering

logger = get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────
# In-memory short-window cache (per process)
# ───────────────────────────────────────────────────────────────────────


@dataclass
class _Bucket:
    """One per (tenant_id, endpoint, user_id) tuple within the current
    rolling window."""
    count: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    per_user: dict[str, int] = field(default_factory=dict)


_WINDOW_SECONDS = 3600          # 1 hour rolling window in memory
_PRUNE_INTERVAL = 600           # prune every 10 minutes


class _MemoryStore:
    """Tiny thread-safe in-memory bucket store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key: (tenant_id, endpoint) → _Bucket
        self._buckets: dict[tuple[str, str], _Bucket] = defaultdict(_Bucket)
        self._last_prune: float = time.time()

    def record(
        self,
        tenant_id: str,
        endpoint: str,
        user_id: str | None,
        bytes_in: int,
        bytes_out: int,
    ) -> None:
        now = time.time()
        with self._lock:
            b = self._buckets[(tenant_id, endpoint)]
            if b.first_ts == 0:
                b.first_ts = now
            b.last_ts = now
            b.count += 1
            b.bytes_in += max(0, bytes_in)
            b.bytes_out += max(0, bytes_out)
            if user_id:
                b.per_user[user_id] = b.per_user.get(user_id, 0) + 1
            if now - self._last_prune > _PRUNE_INTERVAL:
                self._prune_locked(now)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - _WINDOW_SECONDS
        dead = [k for k, v in self._buckets.items() if v.last_ts < cutoff]
        for k in dead:
            self._buckets.pop(k, None)
        self._last_prune = now

    def snapshot(self, tenant_id: str) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            cutoff = now - _WINDOW_SECONDS
            items: list[dict[str, Any]] = []
            for (tid, endpoint), b in self._buckets.items():
                if tid != tenant_id or b.last_ts < cutoff:
                    continue
                items.append({
                    "endpoint": endpoint,
                    "count": b.count,
                    "bytes_in": b.bytes_in,
                    "bytes_out": b.bytes_out,
                    "first_ts": b.first_ts,
                    "last_ts": b.last_ts,
                    "active_users": len(b.per_user),
                })
            return {
                "tenant_id": tenant_id,
                "window_seconds": _WINDOW_SECONDS,
                "now": now,
                "endpoints": items,
            }

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


_store = _MemoryStore()


# ───────────────────────────────────────────────────────────────────────
# UsageMeter — facade
# ───────────────────────────────────────────────────────────────────────


class UsageMeter:
    """High-level façade exposed to the admin portal endpoints and to
    middleware that wants to bill API calls."""

    @staticmethod
    def record(
        tenant_id: str,
        user_id: str | None,
        endpoint: str,
        bytes_in: int = 0,
        bytes_out: int = 0,
        *,
        api_unit: float = 1.0,
    ) -> None:
        """Record a single API hit.

        * Pushes into the legacy meter as ``api_calls`` plus byte counters
          when those metrics are enabled.
        * Updates the in-memory store keyed by ``(tenant, endpoint)``.
        """
        if not tenant_id or not endpoint:
            return

        # ── legacy metering (DB-backed) ───────────────────────────
        metering.record_usage(
            tenant_id, "api_calls", value=api_unit,
            source=f"endpoint:{endpoint}",
            metadata={"endpoint": endpoint, "user_id": user_id or ""},
        )
        if bytes_in > 0:
            metering.record_usage(
                tenant_id, "storage_gb", value=bytes_in / (1024 ** 3),
                source=f"in:{endpoint}",
            ) if "storage_gb" in VALID_USAGE_METRICS else None

        # ── short-window in-memory cache ──────────────────────────
        _store.record(tenant_id, endpoint, user_id, bytes_in, bytes_out)

    @staticmethod
    def record_metric(
        tenant_id: str,
        metric: str,
        value: float = 1.0,
        source: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Forward to the legacy meter, validating the metric name."""
        if metric not in VALID_USAGE_METRICS:
            logger.debug("usage-meter: unknown metric=%s", metric)
            return
        metering.record_usage(
            tenant_id, metric, value=value, source=source, metadata=metadata,
        )

    @staticmethod
    async def get_current(
        db: AsyncSession,
        tenant_id: str,
        period: str = "month",
    ) -> dict[str, Any]:
        """Return aggregated current-period usage by metric.

        ``period`` may be ``"month"`` (calendar month, default) or
        ``"day"`` for a 24-hour rolling window.
        """
        now = datetime.now(timezone.utc)
        if period == "day":
            ps = now - timedelta(days=1)
            pe = now
            stmt = (
                select(UsageRecord.metric, func.sum(UsageRecord.value))
                .where(and_(
                    UsageRecord.workspace_id == tenant_id,
                    UsageRecord.recorded_at >= ps,
                ))
                .group_by(UsageRecord.metric)
            )
            rows = (await db.execute(stmt)).all()
            totals = {m: float(t or 0) for m, t in rows}
        elif period == "month":
            ps = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            totals = await metering.aggregate_usage(
                db, tenant_id, period_start=ps,
                period_end=now.replace(day=28) + timedelta(days=4),
            )
        else:
            totals = await metering.aggregate_usage(db, tenant_id)
            ps = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        live = _store.snapshot(tenant_id)
        return {
            "tenant_id": tenant_id,
            "period": period,
            "period_start": ps.isoformat(),
            "as_of": now.isoformat(),
            "totals": totals,
            "live_endpoints": live["endpoints"],
        }

    @staticmethod
    async def get_history(
        db: AsyncSession,
        tenant_id: str,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
        endpoint: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return time-series usage for ``tenant`` between ``from_dt``
        and ``to_dt``. Daily buckets, all metrics by default.

        If ``endpoint`` is set, we filter the legacy meter's
        ``metadata_json`` for rows whose source/metadata reference that
        endpoint (best-effort substring match; SQLite has no JSON path
        support we can rely on cross-version).
        """
        if to_dt is None:
            to_dt = datetime.now(timezone.utc)
        if from_dt is None:
            from_dt = to_dt - timedelta(days=30)

        stmt = select(UsageRecord).where(and_(
            UsageRecord.workspace_id == tenant_id,
            UsageRecord.recorded_at >= from_dt,
            UsageRecord.recorded_at <= to_dt,
        )).order_by(UsageRecord.recorded_at)
        rows: Iterable[UsageRecord] = (await db.execute(stmt)).scalars().all()

        series: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            if endpoint:
                # Filter by source or metadata.endpoint
                if endpoint not in (r.source or ""):
                    meta = r.metadata_json or {}
                    if str(meta.get("endpoint", "")) != endpoint:
                        continue
            series[r.metric].append({
                "t": r.recorded_at.isoformat() if r.recorded_at else None,
                "v": float(r.value or 0),
                "source": r.source,
            })

        return {
            "tenant_id": tenant_id,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "endpoint": endpoint,
            "series": dict(series),
        }

    @staticmethod
    def live_snapshot(tenant_id: str) -> dict[str, Any]:
        """Return only the in-memory short-window snapshot — useful for
        the live ‘current activity’ widget."""
        return _store.snapshot(tenant_id)

    @staticmethod
    async def flush_now() -> int:
        """Force the legacy buffered meter to flush to disk. Safe to
        call from request handlers right before reading the DB."""
        return await metering.force_flush()


# Module-level convenience singletons ----------------------------------


meter = UsageMeter()
