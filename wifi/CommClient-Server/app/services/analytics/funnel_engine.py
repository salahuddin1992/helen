"""
Funnel computation.

A funnel is an ordered list of events; for each step we count how many
distinct users completed step N within the conversion window after
completing step N-1.

Step shape::

    { "event": "page.view", "filter": { "properties.path": "/pricing" } }
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.analytics import AnalyticsEvent, Funnel
from app.services.analytics.query_engine import _match_filter, _prop_lookup

logger = get_logger(__name__)


async def compute_funnel(
    db: AsyncSession, funnel: Funnel,
    *,
    t_from: datetime | None = None, t_to: datetime | None = None,
) -> dict[str, Any]:
    steps = list(funnel.steps or [])
    if not steps:
        return {"funnel_id": funnel.id, "steps": []}

    window = timedelta(days=int(funnel.conversion_window_days or 7))
    now = datetime.now(timezone.utc)
    t_from = t_from or now - timedelta(days=30)
    t_to = t_to or now

    # Pre-fetch all events for the workspace in window
    event_names = [s.get("event") for s in steps if s.get("event")]
    rows = (await db.execute(
        select(AnalyticsEvent).where(and_(
            AnalyticsEvent.workspace_id == funnel.workspace_id,
            AnalyticsEvent.event_name.in_(event_names),
            AnalyticsEvent.occurred_at >= t_from,
            AnalyticsEvent.occurred_at < t_to,
            AnalyticsEvent.user_id.isnot(None),
        )).order_by(AnalyticsEvent.occurred_at)
    )).scalars().all()

    # Group events per (user, event_name)
    user_event_times: dict[tuple[str, str], list[AnalyticsEvent]] = defaultdict(list)
    for ev in rows:
        user_event_times[(ev.user_id, ev.event_name)].append(ev)

    # Compute funnel step-by-step
    step_users: list[set[str]] = []
    prev_completion: dict[str, datetime] = {}

    for idx, step in enumerate(steps):
        ev_name = step.get("event")
        flt = step.get("filter", {})
        completed_users: dict[str, datetime] = {}
        candidate_users = (set(prev_completion.keys()) if idx > 0
                           else {uid for (uid, en) in user_event_times if en == ev_name})

        for uid in candidate_users:
            events = user_event_times.get((uid, ev_name), [])
            window_start = prev_completion.get(uid) if idx > 0 else t_from
            for ev in events:
                if window_start and ev.occurred_at < window_start:
                    continue
                if window_start and (ev.occurred_at - window_start) > window:
                    break
                if all(
                    _match_filter(
                        _prop_lookup(ev.properties or {}, k), v,
                    ) for k, v in (flt or {}).items()
                ):
                    completed_users[uid] = ev.occurred_at
                    break

        step_users.append(set(completed_users.keys()))
        prev_completion = completed_users

    out_steps: list[dict[str, Any]] = []
    first_count = len(step_users[0]) if step_users else 0
    for idx, (step, users) in enumerate(zip(steps, step_users)):
        prev_count = len(step_users[idx - 1]) if idx > 0 else first_count
        out_steps.append({
            "step": idx + 1,
            "event": step.get("event"),
            "filter": step.get("filter", {}),
            "users": len(users),
            "conversion_from_first_pct": (
                round(100.0 * len(users) / first_count, 2) if first_count else 0
            ),
            "conversion_from_prev_pct": (
                round(100.0 * len(users) / prev_count, 2) if prev_count else 0
            ),
            "drop_off_pct": (
                round(100.0 * (prev_count - len(users)) / prev_count, 2)
                if prev_count else 0
            ),
        })

    snap = {
        "funnel_id": funnel.id, "name": funnel.name,
        "from": t_from.isoformat(), "to": t_to.isoformat(),
        "window_days": funnel.conversion_window_days,
        "steps": out_steps,
    }
    funnel.last_computed_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("funnel.computed id=%s steps=%s", funnel.id, len(out_steps))
    return snap
