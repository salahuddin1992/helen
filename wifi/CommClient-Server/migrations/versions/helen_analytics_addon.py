"""Phase 7 / Module AI — Advanced Analytics & BI tables.

Adds six tables:
    analytics_events
    analytics_dashboards
    analytics_widgets
    analytics_queries
    analytics_cohorts
    analytics_funnels

Revision ID: helen_analytics_addon
Revises: helen_plugins_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_analytics_addon"
down_revision = "helen_plugins_addon"
branch_labels = ("helen_analytics_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("event_name", sa.String(length=128), nullable=False),
        sa.Column("properties", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index("ix_analytics_events_workspace_id", "analytics_events", ["workspace_id"])
    op.create_index("ix_analytics_events_event_name", "analytics_events", ["event_name"])
    op.create_index("ix_analytics_events_user_id", "analytics_events", ["user_id"])
    op.create_index("ix_analytics_events_occurred_at", "analytics_events", ["occurred_at"])
    op.create_index("ix_analytics_events_processed", "analytics_events", ["processed"])

    op.create_table(
        "analytics_dashboards",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("layout", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "slug",
                            name="uq_analytics_dashboards_ws_slug"),
    )
    op.create_index("ix_analytics_dashboards_workspace_id",
                    "analytics_dashboards", ["workspace_id"])

    op.create_table(
        "analytics_widgets",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("dashboard_id", sa.String(length=32),
                  sa.ForeignKey("analytics_dashboards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("position", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_analytics_widgets_dashboard_id",
                    "analytics_widgets", ["dashboard_id"])

    op.create_table(
        "analytics_queries",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("query_dsl", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_by", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_analytics_queries_workspace_id",
                    "analytics_queries", ["workspace_id"])

    op.create_table(
        "analytics_cohorts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("user_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_analytics_cohorts_workspace_id",
                    "analytics_cohorts", ["workspace_id"])

    op.create_table(
        "analytics_funnels",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("conversion_window_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("last_computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_analytics_funnels_workspace_id",
                    "analytics_funnels", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_analytics_funnels_workspace_id", table_name="analytics_funnels")
    op.drop_table("analytics_funnels")

    op.drop_index("ix_analytics_cohorts_workspace_id", table_name="analytics_cohorts")
    op.drop_table("analytics_cohorts")

    op.drop_index("ix_analytics_queries_workspace_id", table_name="analytics_queries")
    op.drop_table("analytics_queries")

    op.drop_index("ix_analytics_widgets_dashboard_id", table_name="analytics_widgets")
    op.drop_table("analytics_widgets")

    op.drop_index("ix_analytics_dashboards_workspace_id",
                  table_name="analytics_dashboards")
    op.drop_table("analytics_dashboards")

    for ix in ("ix_analytics_events_processed", "ix_analytics_events_occurred_at",
               "ix_analytics_events_user_id", "ix_analytics_events_event_name",
               "ix_analytics_events_workspace_id"):
        op.drop_index(ix, table_name="analytics_events")
    op.drop_table("analytics_events")
