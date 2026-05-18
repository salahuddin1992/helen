"""
Stripe provider wrapper.

The ``stripe`` Python SDK is treated as an optional dependency — if the
import fails the wrapper falls back to no-op behaviour so the rest of the
billing system keeps working in self-hosted setups.

Activate by setting:
    HELEN_BILLING_PROVIDER=stripe
    HELEN_STRIPE_API_KEY=sk_...
    HELEN_STRIPE_WEBHOOK_SECRET=whsec_...

The interface mirrors :mod:`app.services.billing.manual_provider` so the
router code can swap providers transparently.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


try:                                                                  # pragma: no cover
    import stripe as _stripe                  # type: ignore[import-untyped]
    _STRIPE_AVAILABLE = True
except Exception:                                                     # noqa: BLE001
    _stripe = None                                                    # type: ignore[assignment]
    _STRIPE_AVAILABLE = False


# ───────────────────────────────────────────────────────────────────────
# Result shapes
# ───────────────────────────────────────────────────────────────────────


@dataclass
class ProviderResult:
    ok: bool
    provider: str
    id: Optional[str] = None
    raw: Optional[dict[str, Any]] = None
    error: Optional[str] = None


# ───────────────────────────────────────────────────────────────────────
# StripeProvider
# ───────────────────────────────────────────────────────────────────────


class StripeProvider:
    """Thin wrapper around the Stripe SDK with graceful degradation."""

    name = "stripe"

    def __init__(
        self,
        api_key: Optional[str] = None,
        webhook_secret: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("HELEN_STRIPE_API_KEY", "")
        self.webhook_secret = (
            webhook_secret or os.getenv("HELEN_STRIPE_WEBHOOK_SECRET", "")
        )
        if _STRIPE_AVAILABLE and self.api_key and _stripe is not None:
            _stripe.api_key = self.api_key

    @property
    def available(self) -> bool:
        return _STRIPE_AVAILABLE and bool(self.api_key) and _stripe is not None

    # ── Customers ─────────────────────────────────────────────────
    def create_customer(
        self, *, email: str, name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProviderResult:
        if not self.available:
            return ProviderResult(False, self.name, error="stripe-unavailable")
        try:
            c = _stripe.Customer.create(                                # type: ignore[union-attr]
                email=email, name=name or "",
                metadata=metadata or {},
            )
            return ProviderResult(True, self.name, id=c.id, raw=dict(c))
        except Exception as e:                                          # noqa: BLE001
            logger.error("stripe.customer.create failed: %s", e)
            return ProviderResult(False, self.name, error=str(e))

    def update_customer(
        self, customer_id: str, **fields: Any,
    ) -> ProviderResult:
        if not self.available:
            return ProviderResult(False, self.name, error="stripe-unavailable")
        try:
            c = _stripe.Customer.modify(customer_id, **fields)          # type: ignore[union-attr]
            return ProviderResult(True, self.name, id=c.id, raw=dict(c))
        except Exception as e:                                          # noqa: BLE001
            return ProviderResult(False, self.name, error=str(e))

    # ── Subscriptions ─────────────────────────────────────────────
    def create_subscription(
        self,
        *,
        customer_id: str,
        price_id: str,
        trial_days: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> ProviderResult:
        if not self.available:
            return ProviderResult(False, self.name, error="stripe-unavailable")
        try:
            kw: dict[str, Any] = {
                "customer": customer_id,
                "items": [{"price": price_id}],
                "metadata": metadata or {},
            }
            if trial_days > 0:
                kw["trial_period_days"] = trial_days
            s = _stripe.Subscription.create(**kw)                       # type: ignore[union-attr]
            return ProviderResult(True, self.name, id=s.id, raw=dict(s))
        except Exception as e:                                          # noqa: BLE001
            return ProviderResult(False, self.name, error=str(e))

    def update_subscription(
        self, sub_id: str, **fields: Any,
    ) -> ProviderResult:
        if not self.available:
            return ProviderResult(False, self.name, error="stripe-unavailable")
        try:
            s = _stripe.Subscription.modify(sub_id, **fields)           # type: ignore[union-attr]
            return ProviderResult(True, self.name, id=s.id, raw=dict(s))
        except Exception as e:                                          # noqa: BLE001
            return ProviderResult(False, self.name, error=str(e))

    def cancel_subscription(
        self, sub_id: str, *, at_period_end: bool = True,
    ) -> ProviderResult:
        if not self.available:
            return ProviderResult(False, self.name, error="stripe-unavailable")
        try:
            if at_period_end:
                s = _stripe.Subscription.modify(                        # type: ignore[union-attr]
                    sub_id, cancel_at_period_end=True,
                )
            else:
                s = _stripe.Subscription.delete(sub_id)                 # type: ignore[union-attr]
            return ProviderResult(True, self.name, id=s.id, raw=dict(s))
        except Exception as e:                                          # noqa: BLE001
            return ProviderResult(False, self.name, error=str(e))

    # ── Invoices ──────────────────────────────────────────────────
    def get_invoice(self, invoice_id: str) -> ProviderResult:
        if not self.available:
            return ProviderResult(False, self.name, error="stripe-unavailable")
        try:
            inv = _stripe.Invoice.retrieve(invoice_id)                  # type: ignore[union-attr]
            return ProviderResult(True, self.name, id=inv.id, raw=dict(inv))
        except Exception as e:                                          # noqa: BLE001
            return ProviderResult(False, self.name, error=str(e))

    # ── Payment methods ──────────────────────────────────────────
    def attach_payment_method(
        self, *, customer_id: str, payment_method_id: str,
    ) -> ProviderResult:
        if not self.available:
            return ProviderResult(False, self.name, error="stripe-unavailable")
        try:
            pm = _stripe.PaymentMethod.attach(                          # type: ignore[union-attr]
                payment_method_id, customer=customer_id,
            )
            return ProviderResult(True, self.name, id=pm.id, raw=dict(pm))
        except Exception as e:                                          # noqa: BLE001
            return ProviderResult(False, self.name, error=str(e))

    def detach_payment_method(self, pm_id: str) -> ProviderResult:
        if not self.available:
            return ProviderResult(False, self.name, error="stripe-unavailable")
        try:
            pm = _stripe.PaymentMethod.detach(pm_id)                    # type: ignore[union-attr]
            return ProviderResult(True, self.name, id=pm.id, raw=dict(pm))
        except Exception as e:                                          # noqa: BLE001
            return ProviderResult(False, self.name, error=str(e))

    # ── Webhook verification ─────────────────────────────────────
    def verify_webhook(
        self, *, payload: bytes, signature_header: str,
        tolerance: int = 300,
    ) -> tuple[bool, Optional[dict[str, Any]]]:
        """Validate Stripe-Signature header per their HMAC scheme."""
        if not self.webhook_secret:
            return False, None

        # If the official SDK is around, defer to it for compatibility.
        if _STRIPE_AVAILABLE and _stripe is not None:
            try:
                evt = _stripe.Webhook.construct_event(                  # type: ignore[union-attr]
                    payload, signature_header, self.webhook_secret,
                    tolerance=tolerance,
                )
                return True, dict(evt)
            except Exception as e:                                      # noqa: BLE001
                logger.warning("stripe.webhook.verify failed: %s", e)
                return False, None

        # Manual verification path (no SDK)
        try:
            parts = dict(
                kv.split("=", 1) for kv in signature_header.split(",")
            )
            ts = int(parts.get("t", "0"))
            sig = parts.get("v1", "")
            signed_payload = f"{ts}.{payload.decode('utf-8')}"
            expected = hmac.new(
                self.webhook_secret.encode("utf-8"),
                signed_payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, sig):
                return False, None
            if tolerance and abs(time.time() - ts) > tolerance:
                return False, None
            import json
            return True, json.loads(payload.decode("utf-8"))
        except Exception as e:                                          # noqa: BLE001
            logger.warning("stripe.webhook.manual-verify failed: %s", e)
            return False, None


# Default singleton — auto-wired at import time.
stripe_provider = StripeProvider()
