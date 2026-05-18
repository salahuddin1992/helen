"""
Admin-portal invoice helpers — thin orchestration on top of the existing
:mod:`app.services.billing.invoice_generator` module.

The existing generator picks every due subscription and writes draft
invoices. The portal needs:

  * generate one invoice for a specific (tenant, period) on demand
  * regenerate an existing invoice (creates a new row, voids the old one)
  * email a generated invoice through the existing notification service
  * stream/read the PDF for download
  * to_pdf — render an Invoice row to a PDF/HTML file on disk

All functions are awaitable; the caller commits via the
``invoice_generator`` helpers it ends up calling.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.billing import (
    Invoice,
    Subscription,
)
from app.services.billing import invoice_generator as _ig
from app.services.billing.invoice_generator import (
    generate_invoice_for_subscription,
    serialize_invoice,
    void as void_invoice,
)

logger = get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────
# InvoiceGenerator façade
# ───────────────────────────────────────────────────────────────────────


class InvoiceGenerator:
    """High-level portal façade with on-demand and per-tenant entry-points.

    Instance is stateless — methods are classmethods so callers don't
    have to construct one explicitly.
    """

    @classmethod
    async def generate(
        cls,
        db: AsyncSession,
        tenant_id: str,
        period: Optional[str] = None,
    ) -> Optional[Invoice]:
        """Generate an invoice for the *current* (or given ISO-8601
        ``YYYY-MM``) period for the active subscription of ``tenant_id``.

        Returns ``None`` if the tenant has no active subscription.
        """
        sub = (await db.execute(
            select(Subscription)
            .where(Subscription.workspace_id == tenant_id)
            .order_by(desc(Subscription.created_at))
        )).scalars().first()
        if sub is None:
            logger.info("portal-invoice: no subscription for tenant=%s", tenant_id)
            return None

        if period:
            try:
                year, month = period.split("-")
                year_i = int(year)
                month_i = int(month)
                ps = datetime(year_i, month_i, 1, tzinfo=timezone.utc)
                if month_i == 12:
                    pe = datetime(year_i + 1, 1, 1, tzinfo=timezone.utc)
                else:
                    pe = datetime(year_i, month_i + 1, 1, tzinfo=timezone.utc)
                sub.current_period_start = ps
                sub.current_period_end = pe
            except (ValueError, IndexError):
                logger.warning("portal-invoice: bad period=%s", period)

        inv = await generate_invoice_for_subscription(db, sub)
        return inv

    @classmethod
    async def regenerate(
        cls,
        db: AsyncSession,
        invoice_id: str,
    ) -> Optional[Invoice]:
        """Void the existing invoice and produce a fresh one for the same
        subscription + period."""
        old = (await db.execute(
            select(Invoice).where(Invoice.id == invoice_id)
        )).scalar_one_or_none()
        if old is None:
            return None
        sub_id = old.subscription_id
        ps, pe = old.period_start, old.period_end

        # void the old row first
        await void_invoice(db, old)

        if sub_id is None:
            return None
        sub = await db.get(Subscription, sub_id)
        if sub is None:
            return None
        sub.current_period_start = ps or sub.current_period_start
        sub.current_period_end = pe or sub.current_period_end
        new = await generate_invoice_for_subscription(db, sub)
        if new:
            md = dict(new.metadata_json or {})
            md["regenerated_from"] = invoice_id
            new.metadata_json = md
            await db.commit()
        return new

    @classmethod
    async def to_pdf(cls, inv: Invoice) -> Path:
        """Render an existing invoice row to a PDF (or HTML fallback)
        path on disk and update ``inv.pdf_url`` to point at it.

        We don't have an open ``AsyncSession`` here, so we synthesise a
        minimal Plan stub for the renderer using the invoice's own
        ``notes`` field (which the generator populates with the plan
        name when it writes the row)."""
        line_specs = [
            _ig._LineSpec(           # type: ignore[attr-defined]
                description=l.description,
                quantity=float(l.quantity),
                unit_price_cents=l.unit_price_cents,
                amount_cents=l.amount_cents,
                metric=l.metric,
                period_start=l.period_start,
                period_end=l.period_end,
            )
            for l in (inv.line_items or [])
        ]

        class _StubPlan:
            name = inv.notes or f"Invoice {inv.number}"
            currency = inv.currency

        path = await _ig._render_pdf(inv, line_specs, _StubPlan(), None)  # type: ignore[arg-type]
        inv.pdf_url = str(path)
        return path

    @classmethod
    async def email(
        cls,
        db: AsyncSession,
        inv: Invoice,
        to_email: Optional[str] = None,
    ) -> bool:
        """Email the invoice via the existing SMTP notification service
        if one is configured. Returns True on success, False if no SMTP
        backend is available.

        We deliberately keep this best-effort: a missing SMTP doesn't
        block the portal — the operator can still download the PDF.
        """
        try:
            from app.services.notification_service import (
                notification_service,
            )
        except Exception:                                              # noqa: BLE001
            notification_service = None  # type: ignore[assignment]

        # Resolve recipient: explicit arg > workspace owner email
        recipient = to_email
        if not recipient:
            try:
                from app.models.workspace import Workspace
                from app.models.user import User
                ws = await db.get(Workspace, inv.workspace_id)
                if ws and ws.owner_id:
                    owner = await db.get(User, ws.owner_id)
                    recipient = getattr(owner, "email", None) or None
            except Exception:                                          # noqa: BLE001
                recipient = None

        if not recipient or notification_service is None:
            logger.info(
                "portal-invoice: email skipped (no recipient/notification service)",
            )
            return False

        try:
            send = getattr(notification_service, "send_email", None)
            if not callable(send):
                return False
            subject = f"Invoice {inv.number} — {inv.currency} {inv.total_cents/100:.2f}"
            body = (
                f"Invoice {inv.number}\n"
                f"Workspace: {inv.workspace_id}\n"
                f"Period: {inv.period_start.date()} → {inv.period_end.date()}\n"
                f"Total: {inv.total_cents} {inv.currency}\n"
            )
            await send(recipient, subject, body, attachments=[inv.pdf_url] if inv.pdf_url else None)
            return True
        except Exception as e:                                         # noqa: BLE001
            logger.error("portal-invoice: email failed err=%s", e)
            return False

    @classmethod
    async def list_for_tenant(
        cls,
        db: AsyncSession,
        tenant_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = (await db.execute(
            select(Invoice).where(Invoice.workspace_id == tenant_id)
            .order_by(desc(Invoice.created_at)).limit(limit)
        )).scalars().all()
        return [serialize_invoice(r) for r in rows]


