"""
Manual provider — for self-hosted deployments without Stripe.

Generates invoices internally; payment is recorded out-of-band (bank
transfer, internal credits, etc.) by an admin clicking "Mark Paid".

Mirrors :class:`StripeProvider` so callers stay provider-agnostic.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from app.core.logging import get_logger
from app.services.billing.stripe_provider import ProviderResult

logger = get_logger(__name__)


class ManualProvider:
    """Synchronous in-process billing provider.

    Subscription / customer / payment-method IDs are randomly generated
    in the ``man_`` prefix so they never collide with Stripe IDs.
    """

    name = "manual"

    @property
    def available(self) -> bool:
        return True

    # ── Customers ─────────────────────────────────────────────────
    def create_customer(
        self, *, email: str, name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProviderResult:
        cid = "man_cus_" + uuid.uuid4().hex[:24]
        return ProviderResult(
            True, self.name, id=cid,
            raw={"id": cid, "email": email, "name": name or "",
                 "metadata": metadata or {}},
        )

    def update_customer(
        self, customer_id: str, **fields: Any,
    ) -> ProviderResult:
        return ProviderResult(True, self.name, id=customer_id, raw=dict(fields))

    # ── Subscriptions ─────────────────────────────────────────────
    def create_subscription(
        self,
        *,
        customer_id: str,
        price_id: str,
        trial_days: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> ProviderResult:
        sid = "man_sub_" + uuid.uuid4().hex[:24]
        return ProviderResult(
            True, self.name, id=sid,
            raw={
                "id": sid,
                "customer": customer_id,
                "price": price_id,
                "trial_days": trial_days,
                "metadata": metadata or {},
                "status": "trialing" if trial_days > 0 else "active",
            },
        )

    def update_subscription(
        self, sub_id: str, **fields: Any,
    ) -> ProviderResult:
        return ProviderResult(True, self.name, id=sub_id, raw=dict(fields))

    def cancel_subscription(
        self, sub_id: str, *, at_period_end: bool = True,
    ) -> ProviderResult:
        return ProviderResult(
            True, self.name, id=sub_id,
            raw={"id": sub_id, "canceled": True,
                 "at_period_end": at_period_end},
        )

    # ── Invoices ──────────────────────────────────────────────────
    def get_invoice(self, invoice_id: str) -> ProviderResult:
        return ProviderResult(True, self.name, id=invoice_id, raw={"id": invoice_id})

    # ── Payment methods ──────────────────────────────────────────
    def attach_payment_method(
        self, *, customer_id: str, payment_method_id: str,
    ) -> ProviderResult:
        return ProviderResult(
            True, self.name, id=payment_method_id,
            raw={"id": payment_method_id, "customer": customer_id},
        )

    def detach_payment_method(self, pm_id: str) -> ProviderResult:
        return ProviderResult(True, self.name, id=pm_id, raw={"id": pm_id, "detached": True})

    # ── Webhook (no-op) ──────────────────────────────────────────
    def verify_webhook(
        self, *, payload: bytes, signature_header: str,
        tolerance: int = 300,
    ) -> tuple[bool, Optional[dict[str, Any]]]:
        return False, None


manual_provider = ManualProvider()
