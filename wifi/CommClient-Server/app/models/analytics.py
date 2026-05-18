"""
Phase 7 / Module AI — Advanced Analytics & BI models.

Six tables:

    analytics_events     — append-only event store
    analytics_dashboards — saved dashboards (workspace-scoped, shareable)
    analytics_widgets    — widget config inside a dashboard
    analytics_queries    — saved ad-hoc queries
    analytics_cohorts    — cohort definitions and pre-computed stats
    analytics_funnels    — funnel definitions
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_WIDGET_KINDS = (
    "line", "bar", "pie", "funnel", "cohort", "retention",
    "table", "kpi", "heatmap", "area", "scatter",
)


# ───────────────────────────────────────────────────────────────────────
# AnalyticsEvent
# ───────────────────────────────────────────────────────────────────────


class AnalyticsEvent(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "analytics_events"
    __table_args__ = (
        Index("ix_analytics_events_workspace_id", "workspace_id"),
        Index("ix_analytics_events_event_name", "event_name"),
        Index("ix_analytics_events_user_id", "user_id"),
        Index("ix_analytics_events_occurred_at", "occurred_at"),
        Index("ix_analytics_events_processed", "processed"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_name: Mapped[str] = mapped_column(String(128), nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )


# ───────────────────────────────────────────────────────────────────────
# Dashboard / Widget
# ───────────────────────────────────────────────────────────────────────


class Dashboard(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "analytics_dashboards"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug",
                         name="uq_analytics_dashboards_ws_slug"),
        Index("ix_analytics_dashboards_workspace_id", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    layout: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    shared: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    created_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    widgets: Mapped[list["Widget"]] = relationship(
        "Widget", back_populates="dashboard",
        cascade="all, delete-orphan", lazy="selectin",
    )


class Widget(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "analytics_widgets"
    __table_args__ = (
        Index("ix_analytics_widgets_dashboard_id", "dashboard_id"),
    )

    dashboard_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("analytics_dashboards.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    position: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )

    dashboard: Mapped[Dashboard] = relationship(
        "Dashboard", back_populates="widgets",
    )


# ───────────────────────────────────────────────────────────────────────
# SavedQuery
# ───────────────────────────────────────────────────────────────────────


class SavedQuery(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "analytics_queries"
    __table_args__ = (
        Index("ix_analytics_queries_workspace_id", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    query_dsl: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    created_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


# ───────────────────────────────────────────────────────────────────────
# Cohort / Funnel
# ───────────────────────────────────────────────────────────────────────


class Cohort(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "analytics_cohorts"
    __table_args__ = (
        Index("ix_analytics_cohorts_workspace_id", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    user_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    retention_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )


class Funnel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "analytics_funnels"
    __table_args__ = (
        Index("ix_analytics_funnels_workspace_id", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    conversion_window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default="7",
    )
    last_computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
