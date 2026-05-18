"""
Cohort & retention engine.

A cohort is the set of users whose **first event** (matching a filter)
happens in a given calendar bucket. Retention is computed as the % of
that cohort that emits any qualifying event in subsequent buckets.

Definition shape:

    {
      "first_event": "user.signed_up",     # or any
      "qualifying_event": "message.sent",  # for retention
      "bucket": "week",                    # day|week|month
      "lookback_days": 90
    }
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.analytics import AnalyticsEvent, Cohort
from app.services.analytics.query_engine import _bucket_truncate

logger = get_logger(__name__)


DEFAULT_BUCKET = "week"
DEFAULT_LOOKBACK = 90


# ───────────────────────────────────────────────────────────────────────
# Cohort computation
# ───────────────────────────────────────────────────────────────────────


async def compute_cohort(
    db: AsyncSession, cohort: Cohort,
) -> dict[str, Any]:
    defn = cohort.definition or {}
    first_event = defn.get("first_event")
    qual_event = defn.get("qualifying_event")
    bucket = defn.get("bucket", DEFAULT_BUCKET)
    lookback = int(defn.get("lookback_days", DEFAULT_LOOKBACK))
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback)

    # Step 1: find each user's first event
    q = select(AnalyticsEvent).where(and_(
        AnalyticsEvent.workspace_id == cohort.workspace_id,
        AnalyticsEvent.occurred_at >= cutoff,
        AnalyticsEvent.user_id.isnot(None),
    ))
    if first_event:
        q = q.where(AnalyticsEvent.event_name == first_event)
    q = q.order_by(AnalyticsEvent.occurred_at)
    rows = (await db.execute(q)).scalars().all()

    user_first: dict[str, datetime] = {}
    for ev in rows:
        uid = ev.user_id
        if not uid:
            continue
        if uid not in user_first or ev.occurred_at < user_first[uid]:
            user_first[uid] = ev.occurred_at

    # Group users by cohort bucket
    cohorts_users: dict[str, set[str]] = defaultdict(set)
    for uid, first in user_first.items():
        cohorts_users[_bucket_truncate(first, bucket).isoformat()].add(uid)

    # Step 2: retention — for each cohort, count distinct users with
    # qualifying events in subsequent buckets
    retention: dict[str, dict[int, int]] = defaultdict(dict)
    user_to_cohort = {
        uid: _bucket_truncate(first, bucket).isoformat()
        for uid, first in user_first.items()
    }
    if user_to_cohort:
        qual_q = select(AnalyticsEvent).where(and_(
            AnalyticsEvent.workspace_id == cohort.workspace_id,
            AnalyticsEvent.occurred_at >= cutoff,
            AnalyticsEvent.user_id.in_(list(user_to_cohort.keys())),
        ))
        if qual_event:
            qual_q = qual_q.where(AnalyticsEvent.event_name == qual_event)
        qual_rows = (await db.execute(qual_q)).scalars().all()
        bucket_seconds = {
            "day": 86400, "week": 604800, "month": 2_592_000,
        }.get(bucket, 86400)
        seen: dict[tuple[str, int], set[str]] = defaultdict(set)
        for ev in qual_rows:
            uid = ev.user_id
            if not uid:
                continue
            cohort_key = user_to_cohort.get(uid)
            if not cohort_key:
                continue
            cohort_dt = datetime.fromisoformat(cohort_key)
            offset = int((ev.occurred_at - cohort_dt).total_seconds() // bucket_seconds)
            if offset < 0:
                continue
            seen[(cohort_key, offset)].add(uid)
        for (cohort_key, offset), uids in seen.items():
            retention[cohort_key][offset] = len(uids)

    # Build output table
    out_cohorts = []
    for cohort_key, uids in sorted(cohorts_users.items()):
        size = len(uids)
        row = {
            "cohort": cohort_key,
            "size": size,
            "retention": {},
        }
        for offset, count in sorted(retention.get(cohort_key, {}).items()):
            row["retention"][offset] = {
                "users": count,
                "pct": round(100.0 * count / size, 2) if size else 0,
            }
        out_cohorts.append(row)

    snap = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "bucket": bucket,
        "cohorts": out_cohorts,
    }
    cohort.retention_snapshot = snap
    cohort.user_count = len(user_first)
    cohort.last_computed_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("cohort.computed ws=%s users=%s buckets=%s",
                cohort.workspace_id, len(user_first), len(out_cohorts))
    return snap
