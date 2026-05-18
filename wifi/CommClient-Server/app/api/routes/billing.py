"""
Phase 7 / Module AG — user-facing billing endpoints.

Mounted under ``/api/billing``. Endpoints are scoped to the caller's
workspace, resolved from their first workspace membership (the same
convention used by other Phase 6/7 modules until a workspace selector
header lands in Phase 8).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Header, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.billing import (
    Coupon,
    Invoice,
    PaymentMethod,
    Plan,
    Subscription,
)
from app.models.workspace import WorkspaceMember
from app.services.billing.dunning import mark_invoice_paid_and_recover
from app.services.billing.invoice_generator import serialize_invoice
from app.services.billing.manual_provider import manual_provider
from app.services.billing.metering import aggregate_usage
from app.services.billing.quota_enforcer import workspace_quota_snapshot
from app.services.billing.stripe_provider import stripe_provider

logger = get_logger(__name__)
router = APIRouter(prefix="/api/billing", tags=["billing"])


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────


def _provider():
    name = os.getenv("HELEN_BILLING_PROVIDER", "manual").lower()
    if name == "stripe" and stripe_provider.available:
        return stripe_provider
    return manual_provider


async def _resolve_workspace(db: AsyncSession, user_id: str) -> str:
    wid = (await db.execute(
        select(WorkspaceMember.workspace_id).where(
            WorkspaceMember.user_id == user_id,
        ).limit(1)
    )).scalar_one_or_none()
    if not wid:
        raise HTTPException(404, detail="no-workspace")
    return wid


# ───────────────────────────────────────────────────────────────────────
# Schemas
# ───────────────────────────────────────────────────────────────────────


class PlanOut(BaseModel):
    id: str
    slug: str
    name: str
    description: Optional[str] = None
    price_monthly_cents: int
    price_yearly_cents: int
    currency: str
    trial_days: int
    included_quotas: dict[str, Any]
    feature_flags: dict[str, Any]


class SubscribeIn(BaseModel):
    plan_slug: str = Field(..., min_length=1, max_length=64)
    billing_cycle: str = Field("monthly", pattern="^(monthly|yearly)$")
    coupon_code: Optional[str] = None
    trial: bool = True


class ChangePlanIn(BaseModel):
    plan_slug: str
    billing_cycle: Optional[str] = Field(None, pattern="^(monthly|yearly)$")


class PaymentMethodIn(BaseModel):
    provider_pm_id: str
    brand: Optional[str] = None
    last4: Optional[str] = None
    exp_month: Optional[int] = None
    exp_year: Optional[int] = None
    is_default: bool = False
    holder_name: Optional[str] = None


class CouponRedeemIn(BaseModel):
    code: str


# ───────────────────────────────────────────────────────────────────────
# Public plan catalogue
# ───────────────────────────────────────────────────────────────────────


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Plan).where(Plan.is_public.is_(True))
        .order_by(Plan.sort_order, Plan.price_monthly_cents)
    )).scalars().all()
    return [PlanOut(
        id=p.id, slug=p.slug, name=p.name, description=p.description,
        price_monthly_cents=p.price_monthly_cents,
        price_yearly_cents=p.price_yearly_cents, currency=p.currency,
        trial_days=p.trial_days,
        included_quotas=dict(p.included_quotas or {}),
        feature_flags=dict(p.feature_flags or {}),
    ) for p in rows]


# ───────────────────────────────────────────────────────────────────────
# Subscription management
# ───────────────────────────────────────────────────────────────────────


@router.get("/me/subscription")
async def my_subscription(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.workspace_id == wid,
            Subscription.status.in_(("active", "trialing", "past_due", "paused")),
        ).order_by(desc(Subscription.started_at))
    )).scalars().first()
    if not sub:
        return {"subscription": None}
    plan = (await db.execute(
        select(Plan).where(Plan.id == sub.plan_id)
    )).scalar_one_or_none()
    return {
        "subscription": {
            "id": sub.id,
            "workspace_id": sub.workspace_id,
            "plan": {
                "id": plan.id, "slug": plan.slug, "name": plan.name,
            } if plan else None,
            "status": sub.status,
            "provider": sub.provider,
            "billing_cycle": sub.billing_cycle,
            "started_at": sub.started_at.isoformat(),
            "current_period_start": sub.current_period_start.isoformat(),
            "current_period_end": sub.current_period_end.isoformat(),
            "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else None,
            "cancel_at": sub.cancel_at.isoformat() if sub.cancel_at else None,
            "coupon_code": sub.coupon_code,
        },
    }


@router.post("/me/subscription")
async def create_subscription(
    body: SubscribeIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    plan = (await db.execute(
        select(Plan).where(Plan.slug == body.plan_slug)
    )).scalar_one_or_none()
    if not plan:
        raise HTTPException(404, "plan-not-found")

    # No overlapping active subs
    existing = (await db.execute(
        select(Subscription).where(
            Subscription.workspace_id == wid,
            Subscription.status.in_(("active", "trialing", "past_due")),
        )
    )).scalars().first()
    if existing:
        raise HTTPException(409, "subscription-already-active")

    coupon_code = None
    if body.coupon_code:
        coupon = (await db.execute(
            select(Coupon).where(Coupon.code == body.coupon_code)
        )).scalar_one_or_none()
        if not coupon or not coupon.valid or coupon.is_expired or coupon.is_exhausted:
            raise HTTPException(400, "invalid-coupon")
        if coupon.applies_to_plans and plan.slug not in coupon.applies_to_plans:
            raise HTTPException(400, "coupon-not-applicable")
        coupon_code = coupon.code

    now = datetime.now(timezone.utc)
    period_days = 365 if body.billing_cycle == "yearly" else 30
    trial_end = (
        now + timedelta(days=plan.trial_days)
        if body.trial and plan.trial_days else None
    )

    provider = _provider()
    pr = provider.create_subscription(
        customer_id=f"workspace_{wid}",
        price_id=plan.slug,
        trial_days=plan.trial_days if body.trial else 0,
        metadata={"workspace_id": wid, "user_id": user_id},
    )

    sub = Subscription(
        id=uuid.uuid4().hex,
        workspace_id=wid,
        plan_id=plan.id,
        status="trialing" if trial_end else "active",
        provider=provider.name,
        provider_subscription_id=pr.id,
        billing_cycle=body.billing_cycle,
        started_at=now,
        current_period_start=now,
        current_period_end=now + timedelta(days=period_days),
        trial_ends_at=trial_end,
        coupon_code=coupon_code,
        metadata_json={"created_by": user_id},
    )
    db.add(sub)
    await db.commit()
    audit_log("billing.subscription.created", user_id=user_id, success=True,
              details={"sub_id": sub.id, "plan": plan.slug})
    return {"id": sub.id, "status": sub.status}


@router.patch("/me/subscription")
async def change_plan(
    body: ChangePlanIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.workspace_id == wid,
            Subscription.status.in_(("active", "trialing", "past_due")),
        )
    )).scalars().first()
    if not sub:
        raise HTTPException(404, "no-subscription")
    plan = (await db.execute(
        select(Plan).where(Plan.slug == body.plan_slug)
    )).scalar_one_or_none()
    if not plan:
        raise HTTPException(404, "plan-not-found")
    sub.plan_id = plan.id
    if body.billing_cycle:
        sub.billing_cycle = body.billing_cycle

    _provider().update_subscription(
        sub.provider_subscription_id or sub.id,
        price_id=plan.slug,
    )
    await db.commit()
    audit_log("billing.subscription.changed", user_id=user_id, success=True,
              details={"sub_id": sub.id, "new_plan": plan.slug})
    return {"ok": True, "plan": plan.slug}


@router.delete("/me/subscription")
async def cancel_subscription(
    at_period_end: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.workspace_id == wid,
            Subscription.status.in_(("active", "trialing", "past_due")),
        )
    )).scalars().first()
    if not sub:
        raise HTTPException(404, "no-subscription")
    now = datetime.now(timezone.utc)
    if at_period_end:
        sub.cancel_at = sub.current_period_end
    else:
        sub.status = "canceled"
        sub.canceled_at = now
    if sub.provider_subscription_id:
        _provider().cancel_subscription(
            sub.provider_subscription_id, at_period_end=at_period_end,
        )
    await db.commit()
    audit_log("billing.subscription.canceled", user_id=user_id, success=True,
              details={"sub_id": sub.id, "at_period_end": at_period_end})
    return {"ok": True, "cancel_at": sub.cancel_at.isoformat() if sub.cancel_at else None}


# ───────────────────────────────────────────────────────────────────────
# Invoices
# ───────────────────────────────────────────────────────────────────────


@router.get("/me/invoices")
async def list_invoices(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    rows = (await db.execute(
        select(Invoice).where(Invoice.workspace_id == wid)
        .order_by(desc(Invoice.created_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {"items": [serialize_invoice(i) for i in rows]}


@router.get("/me/invoices/{invoice_id}/pdf")
async def invoice_pdf(
    invoice_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    inv = (await db.execute(
        select(Invoice).where(
            Invoice.id == invoice_id, Invoice.workspace_id == wid,
        )
    )).scalar_one_or_none()
    if not inv or not inv.pdf_url:
        raise HTTPException(404, "invoice-pdf-missing")
    path = inv.pdf_url
    media_type = "application/pdf" if path.endswith(".pdf") else "text/html"
    try:
        return FileResponse(path, media_type=media_type, filename=f"{inv.number}.{'pdf' if media_type=='application/pdf' else 'html'}")
    except Exception:                                                   # noqa: BLE001
        raise HTTPException(404, "invoice-file-missing")


# ───────────────────────────────────────────────────────────────────────
# Usage
# ───────────────────────────────────────────────────────────────────────


@router.get("/me/usage")
async def my_usage(
    period: str = Query("current", pattern="^(current|prior|ytd)$"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    now = datetime.now(timezone.utc)
    if period == "prior":
        end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (end - timedelta(days=1)).replace(day=1)
        usage = await aggregate_usage(db, wid, period_start=start, period_end=end)
        return {"period": "prior", "start": start.isoformat(),
                "end": end.isoformat(), "usage": usage}
    if period == "ytd":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        usage = await aggregate_usage(db, wid, period_start=start, period_end=now)
        return {"period": "ytd", "usage": usage}
    snap = await workspace_quota_snapshot(db, wid)
    return {"period": "current", **snap}


# ───────────────────────────────────────────────────────────────────────
# Payment methods
# ───────────────────────────────────────────────────────────────────────


@router.get("/me/payment-methods")
async def list_payment_methods(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    rows = (await db.execute(
        select(PaymentMethod).where(PaymentMethod.workspace_id == wid)
        .order_by(desc(PaymentMethod.is_default), desc(PaymentMethod.created_at))
    )).scalars().all()
    return {"items": [
        {
            "id": p.id, "provider": p.provider, "brand": p.brand,
            "last4": p.last4, "exp_month": p.exp_month, "exp_year": p.exp_year,
            "is_default": p.is_default, "holder_name": p.holder_name,
        } for p in rows
    ]}


@router.post("/me/payment-methods")
async def add_payment_method(
    body: PaymentMethodIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    if body.is_default:
        # Demote others
        existing = (await db.execute(
            select(PaymentMethod).where(
                PaymentMethod.workspace_id == wid,
                PaymentMethod.is_default.is_(True),
            )
        )).scalars().all()
        for p in existing:
            p.is_default = False
    provider = _provider()
    pm = PaymentMethod(
        id=uuid.uuid4().hex, workspace_id=wid, provider=provider.name,
        provider_pm_id=body.provider_pm_id, brand=body.brand, last4=body.last4,
        exp_month=body.exp_month, exp_year=body.exp_year,
        is_default=body.is_default, holder_name=body.holder_name,
    )
    db.add(pm)
    await db.commit()
    return {"id": pm.id}


@router.delete("/me/payment-methods/{pm_id}")
async def delete_payment_method(
    pm_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    pm = (await db.execute(
        select(PaymentMethod).where(
            PaymentMethod.id == pm_id, PaymentMethod.workspace_id == wid,
        )
    )).scalar_one_or_none()
    if not pm:
        raise HTTPException(404, "payment-method-not-found")
    _provider().detach_payment_method(pm.provider_pm_id)
    await db.delete(pm)
    await db.commit()
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────────
# Coupons
# ───────────────────────────────────────────────────────────────────────


@router.post("/coupons/redeem")
async def redeem_coupon(
    body: CouponRedeemIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _resolve_workspace(db, user_id)
    coupon = (await db.execute(
        select(Coupon).where(Coupon.code == body.code)
    )).scalar_one_or_none()
    if not coupon or not coupon.valid or coupon.is_expired or coupon.is_exhausted:
        raise HTTPException(400, "invalid-coupon")
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.workspace_id == wid,
            Subscription.status.in_(("active", "trialing", "past_due")),
        )
    )).scalars().first()
    if not sub:
        raise HTTPException(404, "no-subscription")
    sub.coupon_code = coupon.code
    await db.commit()
    audit_log("billing.coupon.redeemed", user_id=user_id, success=True,
              details={"code": coupon.code, "sub_id": sub.id})
    return {"ok": True, "code": coupon.code}


# ───────────────────────────────────────────────────────────────────────
# Stripe webhook
# ───────────────────────────────────────────────────────────────────────


@router.post("/webhook/stripe")
async def stripe_webhook(
    request_body: bytes = Body(...),
    stripe_signature: str = Header("", alias="Stripe-Signature"),
    db: AsyncSession = Depends(get_db),
):
    ok, event = stripe_provider.verify_webhook(
        payload=request_body, signature_header=stripe_signature,
    )
    if not ok or not event:
        raise HTTPException(400, "invalid-signature")
    et = event.get("type", "")
    logger.info("billing.stripe.webhook %s", et)
    obj = event.get("data", {}).get("object", {})
    provider_sub_id = obj.get("subscription") or obj.get("id")

    # Handle a few common events; everything else is acked but not acted on
    if et == "invoice.paid" and provider_sub_id:
        inv = (await db.execute(
            select(Invoice).where(Invoice.provider_invoice_id == obj.get("id"))
        )).scalar_one_or_none()
        if inv:
            await mark_invoice_paid_and_recover(db, inv)
    elif et == "customer.subscription.deleted" and provider_sub_id:
        sub = (await db.execute(
            select(Subscription).where(
                Subscription.provider_subscription_id == provider_sub_id,
            )
        )).scalar_one_or_none()
        if sub:
            sub.status = "canceled"
            sub.canceled_at = datetime.now(timezone.utc)
            await db.commit()
    elif et == "customer.subscription.updated" and provider_sub_id:
        sub = (await db.execute(
            select(Subscription).where(
                Subscription.provider_subscription_id == provider_sub_id,
            )
        )).scalar_one_or_none()
        if sub:
            new_status = obj.get("status")
            if new_status in {"active", "trialing", "past_due", "canceled", "paused"}:
                sub.status = new_status
                await db.commit()

    return Response(status_code=200)
