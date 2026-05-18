"""
Quota enforcement.

Resolves the workspace's active subscription -> plan -> included quota,
compares against current-period usage, and yields a :class:`QuotaResult`.
The :func:`require_quota` factory returns a FastAPI dependency that
blocks the request when a hard limit is exceeded.

Soft vs hard:
    * Soft threshold = 90% of the quota → emit security/billing event.
    * Hard limit = 100% → return 402 Payment Required.

Per-subscription overrides may be supplied through
``Subscription.metadata_json["quota_overrides"]``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.billing import Plan, Subscription
from app.models.workspace import WorkspaceMember
from app.services.billing.metering import aggregate_usage
from app.services.billing.plan_registry import quota_limit_for

logger = get_logger(__name__)


SOFT_THRESHOLD = 0.9


@dataclass
class QuotaResult:
    metric: str
    limit: float            # –1 for unlimited
    used: float
    remaining: float
    allowed: bool
    soft_breach: bool
    plan_slug: Optional[str] = None
    workspace_id: Optional[str] = None


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────


async def _resolve_workspace_for_user(
    db: AsyncSession, user_id: str,
) -> Optional[str]:
    row = (await db.execute(
        select(WorkspaceMember.workspace_id).where(
            WorkspaceMember.user_id == user_id,
        ).limit(1)
    )).scalar_one_or_none()
    return row


async def _active_subscription(
    db: AsyncSession, workspace_id: str,
) -> tuple[Optional[Subscription], Optional[Plan]]:
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.workspace_id == workspace_id,
            Subscription.status.in_(("active", "trialing", "past_due")),
        ).order_by(Subscription.started_at.desc())
    )).scalars().first()
    if not sub:
        return None, None
    plan = (await db.execute(
        select(Plan).where(Plan.id == sub.plan_id)
    )).scalar_one_or_none()
    return sub, plan


def _effective_limit(plan: Plan, sub: Subscription, metric: str) -> float:
    base = quota_limit_for(plan, metric)
    overrides = (sub.metadata_json or {}).get("quota_overrides", {})
    if isinstance(overrides, dict) and metric in overrides:
        try:
            return float(overrides[metric])
        except (TypeError, ValueError):
            pass
    return float(base)


# ───────────────────────────────────────────────────────────────────────
# Core check
# ───────────────────────────────────────────────────────────────────────


async def check_quota(
    db: AsyncSession,
    workspace_id: str,
    metric: str,
    requested: float = 1.0,
) -> QuotaResult:
    """Synchronous quota lookup; does NOT mutate state."""
    sub, plan = await _active_subscription(db, workspace_id)
    if not sub or not plan:
        return QuotaResult(
            metric=metric, limit=0, used=0, remaining=0,
            allowed=False, soft_breach=False,
            workspace_id=workspace_id,
        )

    limit = _effective_limit(plan, sub, metric)
    usage_map = await aggregate_usage(db, workspace_id, metric=metric)
    used = float(usage_map.get(metric, 0))

    if limit < 0:    # unlimited
        return QuotaResult(
            metric=metric, limit=-1, used=used, remaining=math.inf,
            allowed=True, soft_breach=False,
            plan_slug=plan.slug, workspace_id=workspace_id,
        )
    if limit == 0:
        return QuotaResult(
            metric=metric, limit=0, used=used, remaining=0,
            allowed=False, soft_breach=False,
            plan_slug=plan.slug, workspace_id=workspace_id,
        )

    remaining = max(0.0, limit - used)
    allowed = (used + requested) <= limit
    soft_breach = used >= limit * SOFT_THRESHOLD
    return QuotaResult(
        metric=metric, limit=limit, used=used, remaining=remaining,
        allowed=allowed, soft_breach=soft_breach,
        plan_slug=plan.slug, workspace_id=workspace_id,
    )


# ───────────────────────────────────────────────────────────────────────
# FastAPI dependency factory
# ───────────────────────────────────────────────────────────────────────


def require_quota(metric: str, amount: float = 1.0):
    """Block the request with HTTP 402 if quota is exhausted."""
    async def _dep(
        user_id: str = Depends(get_current_user_id),
        db: AsyncSession = Depends(get_db),
    ) -> QuotaResult:
        wid = await _resolve_workspace_for_user(db, user_id)
        if not wid:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="no-workspace-assigned",
            )
        result = await check_quota(db, wid, metric, requested=amount)
        if not result.allowed:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "error": "quota_exceeded",
                    "metric": metric,
                    "limit": result.limit,
                    "used": result.used,
                    "plan": result.plan_slug,
                },
            )
        if result.soft_breach:
            logger.warning(
                "quota.soft-breach workspace=%s metric=%s used=%s/%s",
                wid, metric, result.used, result.limit,
            )
        return result
    return _dep


async def workspace_quota_snapshot(
    db: AsyncSession, workspace_id: str,
) -> dict[str, Any]:
    """Snapshot of every metric in the active plan for dashboards."""
    sub, plan = await _active_subscription(db, workspace_id)
    if not sub or not plan:
        return {"workspace_id": workspace_id, "plan": None, "metrics": {}}
    usage_map = await aggregate_usage(db, workspace_id)
    out: dict[str, Any] = {}
    for metric in (plan.included_quotas or {}).keys():
        limit = _effective_limit(plan, sub, metric)
        used = float(usage_map.get(metric, 0))
        out[metric] = {
            "limit": limit,
            "used": used,
            "remaining": -1 if limit < 0 else max(0.0, limit - used),
            "pct": 0 if limit <= 0 else min(100.0, used * 100.0 / limit),
        }
    return {
        "workspace_id": workspace_id,
        "plan": plan.slug,
        "subscription_status": sub.status,
        "period_start": sub.current_period_start.isoformat(),
        "period_end": sub.current_period_end.isoformat(),
        "metrics": out,
    }
