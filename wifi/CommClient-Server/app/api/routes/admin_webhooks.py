"""
Phase 6 / Module AF — admin webhooks endpoints.

Mounted under ``/api/admin/webhooks``. Requires ``webhooks.admin``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.webhook_v2 import (
    WebhookDeadLetter,
    WebhookDelivery,
    WebhookSubscription,
)
from app.services.rbac.enforcer import require_permission
from app.services.webhooks_v2 import replay
from app.services.webhooks_v2.delivery_engine import delivery_engine

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin/webhooks", tags=["admin-webhooks-v2"])

_PERM = "webhooks.admin"


@router.get("/subscriptions")
async def list_subs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(WebhookSubscription).order_by(desc(WebhookSubscription.created_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "items": [
            {
                "id": s.id, "workspace_id": s.workspace_id, "name": s.name,
                "url": s.url, "enabled": s.enabled,
                "failure_count": s.failure_count,
                "consecutive_failures": s.consecutive_failures,
                "disabled_until": s.disabled_until.isoformat() if s.disabled_until else None,
                "last_delivery_at": s.last_delivery_at.isoformat() if s.last_delivery_at else None,
                "created_by": s.created_by,
                "events": list(s.events or []),
            } for s in rows
        ],
    }


@router.post("/subscriptions/{sub_id}/reset-circuit")
async def reset_circuit(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    s = (await db.execute(
        select(WebhookSubscription).where(WebhookSubscription.id == sub_id)
    )).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, detail="subscription not found")
    s.consecutive_failures = 0
    s.disabled_until = None
    await db.commit()
    audit_log("webhooks_v2.circuit_reset", user_id=user_id, success=True,
              details={"sub_id": sub_id})
    return {"ok": True}


@router.get("/dead-letters")
async def list_dead_letters(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(WebhookDeadLetter).order_by(desc(WebhookDeadLetter.created_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "items": [
            {
                "id": r.id, "delivery_id": r.delivery_id,
                "subscription_id": r.subscription_id,
                "reason": r.reason, "requeued": r.requeued,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r in rows
        ],
    }


@router.post("/dead-letters/{dl_id}/requeue")
async def requeue_dead_letter(
    dl_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    dl = (await db.execute(
        select(WebhookDeadLetter).where(WebhookDeadLetter.id == dl_id)
    )).scalar_one_or_none()
    if dl is None:
        raise HTTPException(404, detail="dead letter not found")
    new_id = await replay.replay_delivery(dl.delivery_id)
    dl.requeued = True
    await db.commit()
    audit_log("webhooks_v2.dl_requeued", user_id=user_id, success=True,
              details={"dl_id": dl_id, "new_delivery_id": new_id})
    return {"new_delivery_id": new_id}


@router.get("/metrics")
async def metrics(
    period_hours: int = Query(24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=period_hours)
    total = (await db.execute(
        select(func.count()).select_from(WebhookDelivery)
        .where(WebhookDelivery.created_at >= cutoff)
    )).scalar_one()
    delivered = (await db.execute(
        select(func.count()).select_from(WebhookDelivery)
        .where(WebhookDelivery.created_at >= cutoff,
               WebhookDelivery.status == "delivered")
    )).scalar_one()
    failed = (await db.execute(
        select(func.count()).select_from(WebhookDelivery)
        .where(WebhookDelivery.created_at >= cutoff,
               WebhookDelivery.status.in_(("failed", "dead")))
    )).scalar_one()

    # p95 latency (rough)
    rows = (await db.execute(
        select(WebhookDelivery.latency_ms).where(
            WebhookDelivery.created_at >= cutoff,
            WebhookDelivery.status == "delivered",
        ).order_by(WebhookDelivery.latency_ms)
    )).scalars().all()
    lats = [int(x) for x in rows if x and x > 0]
    p95 = lats[int(len(lats) * 0.95) - 1] if lats else 0
    success_rate = (int(delivered) / int(total)) if total else None
    return {
        "period_hours": period_hours,
        "total": int(total),
        "delivered": int(delivered),
        "failed": int(failed),
        "success_rate": success_rate,
        "p95_latency_ms": p95,
    }
