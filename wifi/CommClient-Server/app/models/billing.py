"""
Phase 7 / Module AG — Billing & Usage Metering models.

Seven tables describing the full subscription / billing lifecycle:

    billing_plans              — catalogue of subscription plans (Free, Pro, …)
    billing_subscriptions      — per-workspace plan attachment with state
    billing_usage_records      — aggregated usage counters per metric/period
    billing_invoices           — issued invoices (draft → paid)
    billing_invoice_line_items — line breakdown of every invoice
    billing_payment_methods    — saved cards / methods per workspace
    billing_coupons            — promo codes with redemption tracking

Provider can be ``stripe`` or ``manual``; the abstraction in
``app.services.billing.*_provider`` decides which path is taken.

Tenant scoping: every transactional row carries ``workspace_id`` and is
filtered through :mod:`app.services.tenancy.tenant_scope` at query time.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_PROVIDERS = ("stripe", "manual", "paddle", "lemonsqueezy")
VALID_SUBSCRIPTION_STATUSES = (
    "trialing", "active", "past_due", "canceled", "paused", "incomplete",
)
VALID_INVOICE_STATUSES = ("draft", "open", "paid", "void", "uncollectible")
VALID_COUPON_DURATIONS = ("once", "repeating", "forever")
VALID_USAGE_METRICS = (
    "messages_sent",
    "files_uploaded",
    "storage_gb",
    "active_users",
    "ai_tokens",
    "agent_minutes",
    "webhook_deliveries",
    "api_calls",
)


# ───────────────────────────────────────────────────────────────────────
# Plan
# ───────────────────────────────────────────────────────────────────────


class Plan(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "billing_plans"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_billing_plans_slug"),
        Index("ix_billing_plans_is_public", "is_public"),
        Index("ix_billing_plans_sort_order", "sort_order"),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_monthly_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    price_yearly_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="USD", server_default="USD",
    )
    included_quotas: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    feature_flags: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    trial_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict,
    )

    subscriptions: Mapped[list["Subscription"]] = relationship(
        "Subscription", back_populates="plan", lazy="noload",
    )


# ───────────────────────────────────────────────────────────────────────
# Subscription
# ───────────────────────────────────────────────────────────────────────


class Subscription(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "billing_subscriptions"
    __table_args__ = (
        Index("ix_billing_subs_workspace_id", "workspace_id"),
        Index("ix_billing_subs_status", "status"),
        Index("ix_billing_subs_plan_id", "plan_id"),
        Index("ix_billing_subs_period_end", "current_period_end"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("billing_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active",
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual", server_default="manual",
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    provider_customer_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    billing_cycle: Mapped[str] = mapped_column(
        String(16), nullable=False, default="monthly", server_default="monthly",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    cancel_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    coupon_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict,
    )

    plan: Mapped[Plan] = relationship("Plan", back_populates="subscriptions")


# ───────────────────────────────────────────────────────────────────────
# UsageRecord
# ───────────────────────────────────────────────────────────────────────


class UsageRecord(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "billing_usage_records"
    __table_args__ = (
        Index("ix_billing_usage_workspace_id", "workspace_id"),
        Index("ix_billing_usage_metric", "metric"),
        Index("ix_billing_usage_period", "period_start", "period_end"),
        Index("ix_billing_usage_recorded_at", "recorded_at"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(
        Numeric(20, 6), nullable=False, default=0, server_default="0",
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system", server_default="system",
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict,
    )


# ───────────────────────────────────────────────────────────────────────
# Invoice
# ───────────────────────────────────────────────────────────────────────


class Invoice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint("number", name="uq_billing_invoices_number"),
        Index("ix_billing_invoices_workspace_id", "workspace_id"),
        Index("ix_billing_invoices_status", "status"),
        Index("ix_billing_invoices_subscription_id", "subscription_id"),
        Index("ix_billing_invoices_due_at", "due_at"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("billing_subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft",
    )
    total_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    subtotal_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    tax_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    discount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="USD", server_default="USD",
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    voided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual", server_default="manual",
    )
    provider_invoice_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    pdf_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict,
    )

    line_items: Mapped[list["InvoiceLineItem"]] = relationship(
        "InvoiceLineItem", back_populates="invoice",
        cascade="all, delete-orphan", lazy="selectin",
    )


class InvoiceLineItem(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "billing_invoice_line_items"
    __table_args__ = (
        Index("ix_billing_lines_invoice_id", "invoice_id"),
    )

    invoice_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("billing_invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    quantity: Mapped[float] = mapped_column(
        Numeric(20, 6), nullable=False, default=1, server_default="1",
    )
    unit_price_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    amount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    metric: Mapped[str | None] = mapped_column(String(64), nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict,
    )

    invoice: Mapped[Invoice] = relationship("Invoice", back_populates="line_items")


# ───────────────────────────────────────────────────────────────────────
# PaymentMethod
# ───────────────────────────────────────────────────────────────────────


class PaymentMethod(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "billing_payment_methods"
    __table_args__ = (
        Index("ix_billing_pm_workspace_id", "workspace_id"),
        Index("ix_billing_pm_is_default", "is_default"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="stripe", server_default="stripe",
    )
    provider_pm_id: Mapped[str] = mapped_column(String(128), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last4: Mapped[str | None] = mapped_column(String(8), nullable=True)
    exp_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exp_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    holder_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict,
    )


# ───────────────────────────────────────────────────────────────────────
# Coupon
# ───────────────────────────────────────────────────────────────────────


class Coupon(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "billing_coupons"
    __table_args__ = (
        UniqueConstraint("code", name="uq_billing_coupons_code"),
        Index("ix_billing_coupons_valid", "valid"),
        Index("ix_billing_coupons_expires_at", "expires_at"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    percent_off: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_off_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="USD", server_default="USD",
    )
    duration: Mapped[str] = mapped_column(
        String(16), nullable=False, default="once", server_default="once",
    )
    duration_in_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_redemptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redemptions_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    valid: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    applies_to_plans: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict,
    )

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and utc_now() >= self.expires_at

    @property
    def is_exhausted(self) -> bool:
        return (
            self.max_redemptions is not None
            and self.redemptions_count >= self.max_redemptions
        )
