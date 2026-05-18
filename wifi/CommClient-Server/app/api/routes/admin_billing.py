"""
Phase 7 / Module AG — admin billing endpoints.

Mounted under ``/api/admin/billing``. Every route requires the
``billing.manage`` permission via :func:`require_permission`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.billing import (
    Coupon,
    Invoice,
    InvoiceLineItem,
    Plan,
    Subscription,
)
from app.services.billing.invoice_generator import (
    generate_invoices_for_due_subscriptions,
    serialize_invoice,
    void as void_invoice,
)
from app.services.billing.plan_registry import bootstrap_default_plans
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin/billing", tags=["admin-billing"])


_PERM = "billing.manage"


# ───────────────────────────────────────────────────────────────────────
# Schemas
# ───────────────────────────────────────────────────────────────────────


class PlanIn(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64)
    name: str
    description: Optional[str] = None
    price_monthly_cents: int = 0
    price_yearly_cents: int = 0
    currency: str = "USD"
    trial_days: int = 0
    is_public: bool = True
    sort_order: int = 0
    included_quotas: dict[str, Any] = Field(default_factory=dict)
    feature_flags: dict[str, Any] = Field(default_factory=dict)


class CouponIn(BaseModel):
    code: str
    percent_off: Optional[int] = None
    amount_off_cents: Optional[int] = None
    duration: str = Field("once", pattern="^(once|repeating|forever)$")
    duration_in_months: Optional[int] = None
    max_redemptions: Optional[int] = None
    expires_at: Optional[datetime] = None
    applies_to_plans: list[str] = Field(default_factory=list)
    currency: str = "USD"
    valid: bool = True


class CreditIn(BaseModel):
    amount_cents: int = Field(..., gt=0)
    reason: str = ""


# ───────────────────────────────────────────────────────────────────────
# Plans
# ───────────────────────────────────────────────────────────────────────


@router.get("/plans")
async def admin_list_plans(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(Plan).order_by(Plan.sort_order, Plan.price_monthly_cents)
    )).scalars().all()
    return {"items": [
        {
            "id": p.id, "slug": p.slug, "name": p.name,
            "description": p.description,
            "price_monthly_cents": p.price_monthly_cents,
            "price_yearly_cents": p.price_yearly_cents,
            "currency": p.currency, "trial_days": p.trial_days,
            "is_public": p.is_public, "sort_order": p.sort_order,
            "included_quotas": dict(p.included_quotas or {}),
            "feature_flags": dict(p.feature_flags or {}),
        } for p in rows
    ]}


@router.post("/plans")
async def upsert_plan(
    body: PlanIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    p = (await db.execute(
        select(Plan).where(Plan.slug == body.slug)
    )).scalar_one_or_none()
    payload = body.model_dump()
    if p is None:
        p = Plan(**payload)
        db.add(p)
    else:
        for k, v in payload.items():
            setattr(p, k, v)
    await db.commit()
    audit_log("billing.plan.upserted", user_id=user_id, success=True,
              details={"slug": body.slug})
    return {"id": p.id, "slug": p.slug}


@router.post("/plans/bootstrap")
async def bootstrap_plans(
    overwrite: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    return await bootstrap_default_plans(db, overwrite=overwrite)


# ───────────────────────────────────────────────────────────────────────
# Subscriptions
# ───────────────────────────────────────────────────────────────────────


@router.get("/subscriptions")
async def admin_list_subscriptions(
    status_eq: Optional[str] = Query(None, alias="status"),
    workspace_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = select(Subscription)
    if status_eq:
        q = q.where(Subscription.status == status_eq)
    if workspace_id:
        q = q.where(Subscription.workspace_id == workspace_id)
    q = q.order_by(desc(Subscription.started_at)).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return {"items": [
        {
            "id": s.id, "workspace_id": s.workspace_id, "plan_id": s.plan_id,
            "status": s.status, "provider": s.provider,
            "billing_cycle": s.billing_cycle,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "period_end": s.current_period_end.isoformat() if s.current_period_end else None,
            "coupon_code": s.coupon_code,
        } for s in rows
    ]}


@router.post("/subscriptions/{sub_id}/credit")
async def credit_subscription(
    sub_id: str,
    body: CreditIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == sub_id)
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "subscription-not-found")
    # Materialise a paid invoice with a negative amount line (credit)
    now = datetime.now(timezone.utc)
    inv = Invoice(
        id=uuid.uuid4().hex,
        workspace_id=sub.workspace_id, subscription_id=sub.id,
        number="CREDIT-" + uuid.uuid4().hex[:8].upper(),
        status="paid",
        subtotal_cents=-body.amount_cents,
        total_cents=-body.amount_cents,
        currency="USD",
        period_start=now, period_end=now,
        paid_at=now, provider="manual",
        notes=body.reason or "manual credit",
    )
    db.add(inv)
    await db.flush()
    db.add(InvoiceLineItem(
        invoice_id=inv.id, description=f"Credit: {body.reason or 'manual'}",
        quantity=1, unit_price_cents=-body.amount_cents,
        amount_cents=-body.amount_cents,
    ))
    await db.commit()
    audit_log("billing.subscription.credited", user_id=user_id, success=True,
              details={"sub_id": sub.id, "cents": body.amount_cents})
    return {"invoice_id": inv.id, "amount_cents": body.amount_cents}


# ───────────────────────────────────────────────────────────────────────
# Invoices
# ───────────────────────────────────────────────────────────────────────


@router.get("/invoices")
async def admin_list_invoices(
    status_eq: Optional[str] = Query(None, alias="status"),
    workspace_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = select(Invoice)
    if status_eq:
        q = q.where(Invoice.status == status_eq)
    if workspace_id:
        q = q.where(Invoice.workspace_id == workspace_id)
    q = q.order_by(desc(Invoice.created_at)).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return {"items": [serialize_invoice(i) for i in rows]}


@router.post("/invoices/{invoice_id}/void")
async def admin_void_invoice(
    invoice_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    inv = (await db.execute(
        select(Invoice).where(Invoice.id == invoice_id)
    )).scalar_one_or_none()
    if not inv:
        raise HTTPException(404, "invoice-not-found")
    await void_invoice(db, inv)
    audit_log("billing.invoice.voided", user_id=user_id, success=True,
              details={"invoice_id": inv.id})
    return {"ok": True}


@router.post("/invoices/generate-cycle")
async def admin_generate_cycle(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    ids = await generate_invoices_for_due_subscriptions(db)
    audit_log("billing.invoice.cycle", user_id=user_id, success=True,
              details={"count": len(ids)})
    return {"count": len(ids), "invoice_ids": ids}


# ───────────────────────────────────────────────────────────────────────
# Coupons
# ───────────────────────────────────────────────────────────────────────


@router.get("/coupons")
async def admin_list_coupons(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(Coupon).order_by(desc(Coupon.created_at))
    )).scalars().all()
    return {"items": [
        {
            "id": c.id, "code": c.code, "percent_off": c.percent_off,
            "amount_off_cents": c.amount_off_cents,
            "duration": c.duration, "duration_in_months": c.duration_in_months,
            "max_redemptions": c.max_redemptions,
            "redemptions_count": c.redemptions_count,
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            "valid": c.valid,
            "applies_to_plans": list(c.applies_to_plans or []),
        } for c in rows
    ]}


@router.post("/coupons")
async def admin_create_coupon(
    body: CouponIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if not body.percent_off and not body.amount_off_cents:
        raise HTTPException(400, "coupon-needs-discount")
    existing = (await db.execute(
        select(Coupon).where(Coupon.code == body.code)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "coupon-exists")
    c = Coupon(**body.model_dump())
    db.add(c)
    await db.commit()
    audit_log("billing.coupon.created", user_id=user_id, success=True,
              details={"code": body.code})
    return {"id": c.id, "code": c.code}


# ───────────────────────────────────────────────────────────────────────
# Metrics
# ───────────────────────────────────────────────────────────────────────


@router.get("/mrr")
async def mrr_arr(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    """Compute MRR (sum monthly equivalent across active subs) + churn."""
    rows = (await db.execute(
        select(Subscription, Plan).join(Plan, Plan.id == Subscription.plan_id)
        .where(Subscription.status.in_(("active", "trialing", "past_due")))
    )).all()
    mrr_cents = 0
    by_plan: dict[str, int] = {}
    for sub, plan in rows:
        monthly = plan.price_monthly_cents
        if sub.billing_cycle == "yearly":
            monthly = (plan.price_yearly_cents or 0) // 12
        mrr_cents += monthly
        by_plan[plan.slug] = by_plan.get(plan.slug, 0) + monthly

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    churned = (await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.canceled_at.isnot(None),
            Subscription.canceled_at >= cutoff,
        )
    )).scalar_one()
    active = (await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.status.in_(("active", "trialing")),
        )
    )).scalar_one()
    churn_pct = float(churned) / max(1, churned + active) * 100.0
    return {
        "mrr_cents": mrr_cents,
        "arr_cents": mrr_cents * 12,
        "active_subscriptions": active,
        "churned_30d": churned,
        "churn_pct_30d": round(churn_pct, 2),
        "by_plan": by_plan,
    }


@router.get("/usage-summary")
async def admin_usage_summary(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    from app.models.billing import UsageRecord
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(UsageRecord.metric, func.sum(UsageRecord.value))
        .where(UsageRecord.recorded_at >= cutoff)
        .group_by(UsageRecord.metric)
    )).all()
    return {"days": days, "totals": {m: float(t or 0) for m, t in rows}}
