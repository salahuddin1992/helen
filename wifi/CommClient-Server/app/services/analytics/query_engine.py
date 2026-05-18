"""
Query DSL → SQL translator for the analytics event store.

DSL shape (JSON):

    {
      "workspace_id": "...",
      "event": "message.sent",          # or {"$in": [...]}
      "where": {
        "properties.channel_id": "abc",
        "properties.platform": {"$in": ["web","ios"]}
      },
      "from": "2026-04-01T00:00:00Z",
      "to":   "2026-05-01T00:00:00Z",
      "group_by": "day",                # minute|hour|day|week|month, or a property
      "aggregate": "count",             # count|count_distinct|sum|avg|p50|p95|p99
      "aggregate_field": "properties.duration_ms",   # required for sum/avg/percentile
      "limit": 1000
    }

Output: ``[{"bucket": ..., "value": ...}, ...]``
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.analytics import AnalyticsEvent

logger = get_logger(__name__)


SUPPORTED_AGGREGATES = (
    "count", "count_distinct", "sum", "avg",
    "p50", "p95", "p99", "min", "max",
)

SUPPORTED_BUCKETS = ("minute", "hour", "day", "week", "month")


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────


def _parse_iso(s: Any) -> Optional[datetime]:
    if isinstance(s, datetime):
        return s
    if isinstance(s, str):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:                                                   # noqa: BLE001
            return None
    return None


def _bucket_truncate(dt: datetime, bucket: str) -> datetime:
    dt = dt.astimezone(timezone.utc)
    if bucket == "minute":
        return dt.replace(second=0, microsecond=0)
    if bucket == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":
        base = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return base - timedelta(days=base.weekday())
    if bucket == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt


def _prop_lookup(props: dict[str, Any], path: str) -> Any:
    """``properties.platform`` → ``props["platform"]``."""
    if path.startswith("properties."):
        return props.get(path.split(".", 1)[1])
    return None


def _match_filter(value: Any, predicate: Any) -> bool:
    if not isinstance(predicate, dict):
        return value == predicate
    for op, cmp in predicate.items():
        if op == "$eq":
            if value != cmp:
                return False
        elif op == "$ne":
            if value == cmp:
                return False
        elif op == "$in":
            if value not in (cmp or []):
                return False
        elif op == "$nin":
            if value in (cmp or []):
                return False
        elif op == "$gt":
            try:
                if not (value > cmp):
                    return False
            except TypeError:
                return False
        elif op == "$lt":
            try:
                if not (value < cmp):
                    return False
            except TypeError:
                return False
        elif op == "$gte":
            try:
                if not (value >= cmp):
                    return False
            except TypeError:
                return False
        elif op == "$lte":
            try:
                if not (value <= cmp):
                    return False
            except TypeError:
                return False
        elif op == "$between":
            try:
                lo, hi = cmp
                if not (lo <= value <= hi):
                    return False
            except Exception:                                              # noqa: BLE001
                return False
        elif op == "$regex":
            import re
            if not isinstance(value, str) or not re.search(cmp, value):
                return False
        else:
            return False
    return True


# ───────────────────────────────────────────────────────────────────────
# Aggregations (in-Python — DB-portable)
# ───────────────────────────────────────────────────────────────────────


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    k = (len(vs) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(vs) - 1)
    if f == c:
        return float(vs[f])
    return float(vs[f] + (vs[c] - vs[f]) * (k - f))


def _aggregate(rows: list[tuple[Any, float | None]], op: str) -> float:
    values = [float(v) for _, v in rows if v is not None]
    if op == "count":
        return float(len(rows))
    if op == "count_distinct":
        return float(len({b for b, _ in rows}))
    if op == "sum":
        return float(sum(values))
    if op == "avg":
        return float(sum(values) / len(values)) if values else 0.0
    if op == "min":
        return float(min(values)) if values else 0.0
    if op == "max":
        return float(max(values)) if values else 0.0
    if op == "p50":
        return _percentile(values, 50)
    if op == "p95":
        return _percentile(values, 95)
    if op == "p99":
        return _percentile(values, 99)
    raise ValueError(f"unsupported aggregate: {op}")


# ───────────────────────────────────────────────────────────────────────
# Main entrypoint
# ───────────────────────────────────────────────────────────────────────


async def run_query(
    db: AsyncSession, dsl: dict[str, Any],
) -> list[dict[str, Any]]:
    workspace_id = dsl.get("workspace_id")
    if not workspace_id:
        raise ValueError("workspace_id required")
    aggregate = dsl.get("aggregate", "count")
    if aggregate not in SUPPORTED_AGGREGATES:
        raise ValueError(f"unknown aggregate: {aggregate}")
    bucket = dsl.get("group_by", "day")
    limit = int(dsl.get("limit", 1000))
    event_filter = dsl.get("event")
    where_filters: dict[str, Any] = dsl.get("where", {}) or {}
    agg_field = dsl.get("aggregate_field")
    t_from = _parse_iso(dsl.get("from"))
    t_to = _parse_iso(dsl.get("to"))

    stmt = select(AnalyticsEvent).where(AnalyticsEvent.workspace_id == workspace_id)
    if event_filter:
        if isinstance(event_filter, str):
            stmt = stmt.where(AnalyticsEvent.event_name == event_filter)
        elif isinstance(event_filter, dict) and "$in" in event_filter:
            stmt = stmt.where(AnalyticsEvent.event_name.in_(list(event_filter["$in"])))
    if t_from:
        stmt = stmt.where(AnalyticsEvent.occurred_at >= t_from)
    if t_to:
        stmt = stmt.where(AnalyticsEvent.occurred_at < t_to)
    stmt = stmt.order_by(AnalyticsEvent.occurred_at).limit(max(limit, 1) * 50)

    rows = (await db.execute(stmt)).scalars().all()

    # Apply property-level filters in Python (portable across SQLite/Postgres)
    filtered: list[AnalyticsEvent] = []
    for ev in rows:
        ok = True
        for key, pred in where_filters.items():
            val = _prop_lookup(ev.properties or {}, key)
            if not _match_filter(val, pred):
                ok = False
                break
        if ok:
            filtered.append(ev)

    # Bucketize
    is_time_bucket = bucket in SUPPORTED_BUCKETS
    grouped: dict[Any, list[tuple[Any, float | None]]] = {}
    for ev in filtered:
        if is_time_bucket:
            key = _bucket_truncate(ev.occurred_at, bucket).isoformat()
        elif bucket.startswith("properties."):
            key = _prop_lookup(ev.properties or {}, bucket)
        elif bucket == "event":
            key = ev.event_name
        elif bucket == "user_id":
            key = ev.user_id
        else:
            key = "*"
        if agg_field:
            v = _prop_lookup(ev.properties or {}, agg_field)
            try:
                v = float(v) if v is not None else None
            except (TypeError, ValueError):
                v = None
            grouped.setdefault(key, []).append((ev.user_id, v))
        else:
            grouped.setdefault(key, []).append((ev.user_id, 1.0))

    out: list[dict[str, Any]] = []
    for key, pairs in grouped.items():
        out.append({
            "bucket": key,
            "value": _aggregate(pairs, aggregate),
            "count": len(pairs),
        })
    out.sort(key=lambda x: (str(x.get("bucket") or "")))
    return out[:limit]


# ───────────────────────────────────────────────────────────────────────
# Top-N / breakdown helpers
# ───────────────────────────────────────────────────────────────────────


async def top_events(
    db: AsyncSession, workspace_id: str, *, days: int = 7, limit: int = 20,
) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(AnalyticsEvent.event_name, func.count(AnalyticsEvent.id))
        .where(and_(AnalyticsEvent.workspace_id == workspace_id,
                    AnalyticsEvent.occurred_at >= cutoff))
        .group_by(AnalyticsEvent.event_name)
        .order_by(func.count(AnalyticsEvent.id).desc())
        .limit(limit)
    )).all()
    return [{"event": e, "count": int(c or 0)} for e, c in rows]
