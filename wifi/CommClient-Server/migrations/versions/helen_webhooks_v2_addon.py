"""Phase 6 / Module AF — Webhooks v2 tables.

Adds three tables:
    webhook_v2_subscriptions
    webhook_v2_deliveries
    webhook_v2_dead_letters

Revision ID: helen_webhooks_v2_addon
Revises: helen_compliance_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_webhooks_v2_addon"
down_revision = "helen_compliance_addon"
branch_labels = ("helen_webhooks_v2_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_v2_subscriptions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("secret", sa.String(length=128), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("filters", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("created_by", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disabled_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_wv2_subs_workspace_id",
                    "webhook_v2_subscriptions", ["workspace_id"])
    op.create_index("ix_wv2_subs_enabled",
                    "webhook_v2_subscriptions", ["enabled"])

    op.create_table(
        "webhook_v2_deliveries",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("subscription_id", sa.String(length=32),
                  sa.ForeignKey("webhook_v2_subscriptions.id",
                                ondelete="CASCADE"),
                  nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_wv2_deliv_subscription_id",
                    "webhook_v2_deliveries", ["subscription_id"])
    op.create_index("ix_wv2_deliv_status",
                    "webhook_v2_deliveries", ["status"])
    op.create_index("ix_wv2_deliv_created_at",
                    "webhook_v2_deliveries", ["created_at"])
    op.create_index("ix_wv2_deliv_event_type",
                    "webhook_v2_deliveries", ["event_type"])

    op.create_table(
        "webhook_v2_dead_letters",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("delivery_id", sa.String(length=32),
                  sa.ForeignKey("webhook_v2_deliveries.id",
                                ondelete="CASCADE"),
                  nullable=False),
        sa.Column("subscription_id", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("requeued", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_wv2_dlq_created_at",
                    "webhook_v2_dead_letters", ["created_at"])
    op.create_index("ix_wv2_dlq_delivery_id",
                    "webhook_v2_dead_letters", ["delivery_id"])


def downgrade() -> None:
    op.drop_index("ix_wv2_dlq_delivery_id",
                  table_name="webhook_v2_dead_letters")
    op.drop_index("ix_wv2_dlq_created_at",
                  table_name="webhook_v2_dead_letters")
    op.drop_table("webhook_v2_dead_letters")

    op.drop_index("ix_wv2_deliv_event_type",
                  table_name="webhook_v2_deliveries")
    op.drop_index("ix_wv2_deliv_created_at",
                  table_name="webhook_v2_deliveries")
    op.drop_index("ix_wv2_deliv_status",
                  table_name="webhook_v2_deliveries")
    op.drop_index("ix_wv2_deliv_subscription_id",
                  table_name="webhook_v2_deliveries")
    op.drop_table("webhook_v2_deliveries")

    op.drop_index("ix_wv2_subs_enabled",
                  table_name="webhook_v2_subscriptions")
    op.drop_index("ix_wv2_subs_workspace_id",
                  table_name="webhook_v2_subscriptions")
    op.drop_table("webhook_v2_subscriptions")
