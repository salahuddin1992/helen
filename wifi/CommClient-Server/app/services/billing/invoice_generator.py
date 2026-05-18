"""
Invoice generator.

Walks each active subscription whose ``current_period_end`` is in the
past and produces an :class:`Invoice` row + line items derived from:

    * the subscription's base plan price (flat fee)
    * any overage line items (usage > quota → metered pricing)
    * coupon discounts attached to the subscription
    * tax (flat % from env or per-workspace metadata)

PDF rendering is optional: when ``reportlab`` is installed we generate a
real PDF; otherwise we write an HTML fallback that browsers can print.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.billing import (
    Coupon,
    Invoice,
    InvoiceLineItem,
    Plan,
    Subscription,
)
from app.services.billing.metering import aggregate_usage
from app.services.billing.plan_registry import quota_limit_for

logger = get_logger(__name__)


# Optional PDF dep
try:                                                                  # pragma: no cover
    from reportlab.lib.pagesizes import letter            # type: ignore[import-untyped]
    from reportlab.lib.styles import getSampleStyleSheet  # type: ignore[import-untyped]
    from reportlab.platypus import (                       # type: ignore[import-untyped]
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )
    from reportlab.lib import colors                       # type: ignore[import-untyped]
    _REPORTLAB_AVAILABLE = True
except Exception:                                                     # noqa: BLE001
    _REPORTLAB_AVAILABLE = False


DEFAULT_TAX_PCT = float(os.getenv("HELEN_BILLING_TAX_PCT", "0"))
INVOICE_DIR = Path(os.getenv("HELEN_INVOICE_DIR", "data/invoices"))
INVOICE_DIR.mkdir(parents=True, exist_ok=True)


# Per-metric overage pricing in cents (cost per 1 unit beyond quota).
OVERAGE_PRICING: dict[str, float] = {
    "messages_sent": 0.001,        # $0.00001 / msg ≈ negligible per msg
    "files_uploaded": 0.5,         # 0.5¢ / file
    "storage_gb": 50.0,            # 50¢ / GB / month
    "ai_tokens": 0.01,             # 1¢ / 1k tokens (we charge per token /100)
    "agent_minutes": 5.0,          # 5¢ / minute
    "webhook_deliveries": 0.001,
    "api_calls": 0.0001,
}


# ───────────────────────────────────────────────────────────────────────
# Result dataclasses
# ───────────────────────────────────────────────────────────────────────


@dataclass
class _LineSpec:
    description: str
    quantity: float
    unit_price_cents: int
    amount_cents: int
    metric: Optional[str] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None


# ───────────────────────────────────────────────────────────────────────
# Pricing pipeline
# ───────────────────────────────────────────────────────────────────────


def _next_invoice_number() -> str:
    return "INV-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6].upper()


def _base_line(plan: Plan, sub: Subscription) -> _LineSpec:
    if sub.billing_cycle == "yearly":
        amount = plan.price_yearly_cents or 0
        desc = f"{plan.name} plan (yearly)"
    else:
        amount = plan.price_monthly_cents or 0
        desc = f"{plan.name} plan (monthly)"
    return _LineSpec(
        description=desc, quantity=1.0,
        unit_price_cents=amount, amount_cents=amount,
        period_start=sub.current_period_start, period_end=sub.current_period_end,
    )


async def _overage_lines(
    db: AsyncSession, plan: Plan, sub: Subscription,
) -> list[_LineSpec]:
    usage = await aggregate_usage(
        db, sub.workspace_id,
        period_start=sub.current_period_start,
        period_end=sub.current_period_end,
    )
    out: list[_LineSpec] = []
    for metric, used in usage.items():
        limit = quota_limit_for(plan, metric)
        if limit < 0 or limit == 0:
            continue
        overage = used - limit
        if overage <= 0:
            continue
        rate = OVERAGE_PRICING.get(metric, 0)
        if rate <= 0:
            continue
        amount = int(round(overage * rate))
        if amount <= 0:
            continue
        out.append(_LineSpec(
            description=f"Overage: {metric} ({overage:.0f} units)",
            quantity=overage,
            unit_price_cents=int(round(rate)),
            amount_cents=amount,
            metric=metric,
            period_start=sub.current_period_start,
            period_end=sub.current_period_end,
        ))
    return out


async def _coupon_discount(
    db: AsyncSession, sub: Subscription, subtotal_cents: int,
) -> tuple[int, Optional[Coupon]]:
    if not sub.coupon_code:
        return 0, None
    coupon = (await db.execute(
        select(Coupon).where(Coupon.code == sub.coupon_code)
    )).scalar_one_or_none()
    if not coupon or not coupon.valid or coupon.is_expired or coupon.is_exhausted:
        return 0, None
    if coupon.percent_off:
        return int(round(subtotal_cents * (coupon.percent_off / 100.0))), coupon
    if coupon.amount_off_cents:
        return min(subtotal_cents, int(coupon.amount_off_cents)), coupon
    return 0, coupon


def _tax_cents(workspace_meta: dict[str, Any], taxable_cents: int) -> int:
    rate = workspace_meta.get("tax_pct", DEFAULT_TAX_PCT)
    try:
        rate_f = float(rate)
    except (TypeError, ValueError):
        rate_f = 0.0
    if rate_f <= 0:
        return 0
    return int(round(taxable_cents * (rate_f / 100.0)))


# ───────────────────────────────────────────────────────────────────────
# Invoice creation
# ───────────────────────────────────────────────────────────────────────


async def generate_invoice_for_subscription(
    db: AsyncSession, sub: Subscription,
) -> Optional[Invoice]:
    """Produce a draft invoice for one subscription's current period."""
    plan = (await db.execute(
        select(Plan).where(Plan.id == sub.plan_id)
    )).scalar_one_or_none()
    if not plan:
        logger.warning("invoice.gen: plan-missing sub=%s", sub.id)
        return None

    lines: list[_LineSpec] = [_base_line(plan, sub)]
    lines.extend(await _overage_lines(db, plan, sub))

    subtotal = sum(l.amount_cents for l in lines)
    if subtotal <= 0 and len(lines) == 1 and lines[0].amount_cents == 0:
        # Free plan with no overages — no invoice needed
        return None

    discount_cents, coupon = await _coupon_discount(db, sub, subtotal)
    taxable = max(0, subtotal - discount_cents)
    tax = _tax_cents(sub.metadata_json or {}, taxable)
    total = max(0, subtotal - discount_cents + tax)

    inv = Invoice(
        workspace_id=sub.workspace_id,
        subscription_id=sub.id,
        number=_next_invoice_number(),
        status="open",
        subtotal_cents=subtotal,
        discount_cents=discount_cents,
        tax_cents=tax,
        total_cents=total,
        currency=plan.currency or "USD",
        period_start=sub.current_period_start,
        period_end=sub.current_period_end,
        due_at=sub.current_period_end,
        provider=sub.provider,
    )
    db.add(inv)
    await db.flush()

    for line in lines:
        db.add(InvoiceLineItem(
            invoice_id=inv.id,
            description=line.description,
            quantity=line.quantity,
            unit_price_cents=line.unit_price_cents,
            amount_cents=line.amount_cents,
            metric=line.metric,
            period_start=line.period_start,
            period_end=line.period_end,
        ))

    # PDF render
    try:
        pdf_path = await _render_pdf(inv, lines, plan, coupon)
        inv.pdf_url = str(pdf_path)
    except Exception as e:                                              # noqa: BLE001
        logger.error("invoice.pdf-failed inv=%s err=%s", inv.id, e)

    # Bump coupon redemptions
    if coupon:
        coupon.redemptions_count += 1

    await db.commit()
    logger.info("invoice.generated inv=%s ws=%s total=%s",
                inv.id, sub.workspace_id, total)
    return inv


async def generate_invoices_for_due_subscriptions(
    db: AsyncSession,
) -> list[str]:
    now = datetime.now(timezone.utc)
    due = (await db.execute(
        select(Subscription).where(
            Subscription.status.in_(("active", "past_due", "trialing")),
            Subscription.current_period_end <= now,
        )
    )).scalars().all()
    ids: list[str] = []
    for sub in due:
        inv = await generate_invoice_for_subscription(db, sub)
        if inv:
            ids.append(inv.id)
    return ids


# ───────────────────────────────────────────────────────────────────────
# PDF / HTML rendering
# ───────────────────────────────────────────────────────────────────────


async def _render_pdf(
    inv: Invoice,
    lines: Iterable[_LineSpec],
    plan: Plan,
    coupon: Optional[Coupon],
) -> Path:
    if _REPORTLAB_AVAILABLE:
        return _render_pdf_reportlab(inv, lines, plan, coupon)
    return _render_html_fallback(inv, lines, plan, coupon)


def _render_pdf_reportlab(
    inv: Invoice, lines: Iterable[_LineSpec], plan: Plan,
    coupon: Optional[Coupon],
) -> Path:                                                            # pragma: no cover
    path = INVOICE_DIR / f"{inv.number}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    styles = getSampleStyleSheet()
    story: list[Any] = []
    story.append(Paragraph(f"Invoice {inv.number}", styles["Title"]))
    story.append(Paragraph(
        f"Period {inv.period_start.date()} → {inv.period_end.date()}",
        styles["Normal"],
    ))
    story.append(Spacer(1, 12))

    data = [["Description", "Qty", "Unit (¢)", "Amount (¢)"]]
    for l in lines:
        data.append([l.description, f"{l.quantity:.2f}",
                     str(l.unit_price_cents), str(l.amount_cents)])
    data.append(["", "", "Subtotal", str(inv.subtotal_cents)])
    if inv.discount_cents:
        cstr = coupon.code if coupon else "discount"
        data.append(["", "", f"Discount ({cstr})", f"-{inv.discount_cents}"])
    data.append(["", "", "Tax", str(inv.tax_cents)])
    data.append(["", "", "TOTAL", str(inv.total_cents)])

    t = Table(data, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    story.append(t)
    doc.build(story)
    return path


def _render_html_fallback(
    inv: Invoice, lines: Iterable[_LineSpec], plan: Plan,
    coupon: Optional[Coupon],
) -> Path:
    path = INVOICE_DIR / f"{inv.number}.html"
    rows_html = "".join(
        f"<tr><td>{l.description}</td><td>{l.quantity:.2f}</td>"
        f"<td>{l.unit_price_cents}</td><td>{l.amount_cents}</td></tr>"
        for l in lines
    )
    discount_row = (
        f"<tr><td colspan='3'><b>Discount</b></td>"
        f"<td>-{inv.discount_cents}¢</td></tr>"
        if inv.discount_cents else ""
    )
    path.write_text(f"""<!doctype html>
<meta charset="utf-8"><title>Invoice {inv.number}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 32px; color:#222 }}
 h1 {{ font-size:22px }}
 table {{ border-collapse:collapse; width:100% }}
 th, td {{ border:1px solid #ccc; padding:6px 10px; text-align:left }}
 tfoot td {{ font-weight:600 }}
</style>
<h1>Invoice {inv.number}</h1>
<p>Plan: <b>{plan.name}</b> · Workspace: <code>{inv.workspace_id}</code></p>
<p>Period: {inv.period_start.date()} → {inv.period_end.date()}</p>
<table>
 <thead><tr><th>Description</th><th>Qty</th><th>Unit ¢</th><th>Amount ¢</th></tr></thead>
 <tbody>{rows_html}</tbody>
 <tfoot>
  <tr><td colspan='3'><b>Subtotal</b></td><td>{inv.subtotal_cents}¢</td></tr>
  {discount_row}
  <tr><td colspan='3'><b>Tax</b></td><td>{inv.tax_cents}¢</td></tr>
  <tr><td colspan='3'><b>TOTAL</b></td><td>{inv.total_cents}¢ {inv.currency}</td></tr>
 </tfoot>
</table>
""", encoding="utf-8")
    return path


# ───────────────────────────────────────────────────────────────────────
# Lifecycle helpers
# ───────────────────────────────────────────────────────────────────────


async def mark_paid(db: AsyncSession, inv: Invoice) -> None:
    inv.status = "paid"
    inv.paid_at = datetime.now(timezone.utc)
    await db.commit()


async def void(db: AsyncSession, inv: Invoice) -> None:
    inv.status = "void"
    inv.voided_at = datetime.now(timezone.utc)
    await db.commit()


def serialize_invoice(inv: Invoice) -> dict[str, Any]:
    return {
        "id": inv.id,
        "number": inv.number,
        "status": inv.status,
        "workspace_id": inv.workspace_id,
        "subscription_id": inv.subscription_id,
        "subtotal_cents": inv.subtotal_cents,
        "discount_cents": inv.discount_cents,
        "tax_cents": inv.tax_cents,
        "total_cents": inv.total_cents,
        "currency": inv.currency,
        "period_start": inv.period_start.isoformat() if inv.period_start else None,
        "period_end": inv.period_end.isoformat() if inv.period_end else None,
        "due_at": inv.due_at.isoformat() if inv.due_at else None,
        "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
        "provider": inv.provider,
        "pdf_url": inv.pdf_url,
        "line_items": [
            {
                "description": l.description, "quantity": float(l.quantity),
                "unit_price_cents": l.unit_price_cents,
                "amount_cents": l.amount_cents, "metric": l.metric,
            }
            for l in (inv.line_items or [])
        ],
    }
