"""Phase 7 / Module AG — Billing & Usage Metering tables.

Adds seven tables:
    billing_plans
    billing_subscriptions
    billing_usage_records
    billing_invoices
    billing_invoice_line_items
    billing_payment_methods
    billing_coupons

Revision ID: helen_billing_addon
Revises: helen_security_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_billing_addon"
down_revision = "helen_security_addon"
branch_labels = ("helen_billing_addon",)
depends_on = None


def upgrade() -> None:
    # ── billing_plans ────────────────────────────────────────────
    op.create_table(
        "billing_plans",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False, unique=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_monthly_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price_yearly_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("included_quotas", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("feature_flags", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_billing_plans_is_public", "billing_plans", ["is_public"])
    op.create_index("ix_billing_plans_sort_order", "billing_plans", ["sort_order"])

    # ── billing_subscriptions ───────────────────────────────────
    op.create_table(
        "billing_subscriptions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.String(length=32),
                  sa.ForeignKey("billing_plans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("provider_subscription_id", sa.String(length=128), nullable=True),
        sa.Column("provider_customer_id", sa.String(length=128), nullable=True),
        sa.Column("billing_cycle", sa.String(length=16), nullable=False, server_default="monthly"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("cancel_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coupon_code", sa.String(length=64), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_billing_subs_workspace_id", "billing_subscriptions", ["workspace_id"])
    op.create_index("ix_billing_subs_status", "billing_subscriptions", ["status"])
    op.create_index("ix_billing_subs_plan_id", "billing_subscriptions", ["plan_id"])
    op.create_index("ix_billing_subs_period_end", "billing_subscriptions", ["current_period_end"])

    # ── billing_usage_records ───────────────────────────────────
    op.create_table(
        "billing_usage_records",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index("ix_billing_usage_workspace_id", "billing_usage_records", ["workspace_id"])
    op.create_index("ix_billing_usage_metric", "billing_usage_records", ["metric"])
    op.create_index("ix_billing_usage_period", "billing_usage_records",
                    ["period_start", "period_end"])
    op.create_index("ix_billing_usage_recorded_at", "billing_usage_records", ["recorded_at"])

    # ── billing_invoices ────────────────────────────────────────
    op.create_table(
        "billing_invoices",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_id", sa.String(length=32),
                  sa.ForeignKey("billing_subscriptions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("number", sa.String(length=64), nullable=False, unique=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subtotal_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tax_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("provider_invoice_id", sa.String(length=128), nullable=True),
        sa.Column("pdf_url", sa.String(length=2048), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_billing_invoices_workspace_id", "billing_invoices", ["workspace_id"])
    op.create_index("ix_billing_invoices_status", "billing_invoices", ["status"])
    op.create_index("ix_billing_invoices_subscription_id", "billing_invoices", ["subscription_id"])
    op.create_index("ix_billing_invoices_due_at", "billing_invoices", ["due_at"])

    # ── billing_invoice_line_items ──────────────────────────────
    op.create_table(
        "billing_invoice_line_items",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("invoice_id", sa.String(length=32),
                  sa.ForeignKey("billing_invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False, server_default="1"),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metric", sa.String(length=64), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index("ix_billing_lines_invoice_id", "billing_invoice_line_items", ["invoice_id"])

    # ── billing_payment_methods ─────────────────────────────────
    op.create_table(
        "billing_payment_methods",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="stripe"),
        sa.Column("provider_pm_id", sa.String(length=128), nullable=False),
        sa.Column("brand", sa.String(length=32), nullable=True),
        sa.Column("last4", sa.String(length=8), nullable=True),
        sa.Column("exp_month", sa.Integer(), nullable=True),
        sa.Column("exp_year", sa.Integer(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("holder_name", sa.String(length=128), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_billing_pm_workspace_id", "billing_payment_methods", ["workspace_id"])
    op.create_index("ix_billing_pm_is_default", "billing_payment_methods", ["is_default"])

    # ── billing_coupons ─────────────────────────────────────────
    op.create_table(
        "billing_coupons",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False, unique=True),
        sa.Column("percent_off", sa.Integer(), nullable=True),
        sa.Column("amount_off_cents", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("duration", sa.String(length=16), nullable=False, server_default="once"),
        sa.Column("duration_in_months", sa.Integer(), nullable=True),
        sa.Column("max_redemptions", sa.Integer(), nullable=True),
        sa.Column("redemptions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("applies_to_plans", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_billing_coupons_valid", "billing_coupons", ["valid"])
    op.create_index("ix_billing_coupons_expires_at", "billing_coupons", ["expires_at"])


def downgrade() -> None:
    for ix in ("ix_billing_coupons_expires_at", "ix_billing_coupons_valid"):
        op.drop_index(ix, table_name="billing_coupons")
    op.drop_table("billing_coupons")

    for ix in ("ix_billing_pm_is_default", "ix_billing_pm_workspace_id"):
        op.drop_index(ix, table_name="billing_payment_methods")
    op.drop_table("billing_payment_methods")

    op.drop_index("ix_billing_lines_invoice_id",
                  table_name="billing_invoice_line_items")
    op.drop_table("billing_invoice_line_items")

    for ix in ("ix_billing_invoices_due_at", "ix_billing_invoices_subscription_id",
               "ix_billing_invoices_status", "ix_billing_invoices_workspace_id"):
        op.drop_index(ix, table_name="billing_invoices")
    op.drop_table("billing_invoices")

    for ix in ("ix_billing_usage_recorded_at", "ix_billing_usage_period",
               "ix_billing_usage_metric", "ix_billing_usage_workspace_id"):
        op.drop_index(ix, table_name="billing_usage_records")
    op.drop_table("billing_usage_records")

    for ix in ("ix_billing_subs_period_end", "ix_billing_subs_plan_id",
               "ix_billing_subs_status", "ix_billing_subs_workspace_id"):
        op.drop_index(ix, table_name="billing_subscriptions")
    op.drop_table("billing_subscriptions")

    for ix in ("ix_billing_plans_sort_order", "ix_billing_plans_is_public"):
        op.drop_index(ix, table_name="billing_plans")
    op.drop_table("billing_plans")
