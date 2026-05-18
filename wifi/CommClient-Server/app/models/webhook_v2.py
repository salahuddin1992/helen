"""
Phase 6 / Module AF — Webhooks v2 models.

Three tables:
    webhook_v2_subscriptions
    webhook_v2_deliveries
    webhook_v2_dead_letters

Naming uses the ``v2`` suffix so existing ``webhooks`` / ``webhook_deliveries``
tables remain untouched.
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_WV2_DELIVERY_STATUSES = ("pending", "in_flight", "delivered", "failed", "dead")


class WebhookSubscription(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "webhook_v2_subscriptions"
    __table_args__ = (
        Index("ix_wv2_subs_workspace_id", "workspace_id"),
        Index("ix_wv2_subs_enabled", "enabled"),
    )

    workspace_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    events: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    created_by: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    disabled_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    deliveries: Mapped[list["WebhookDelivery"]] = relationship(
        "WebhookDelivery", back_populates="subscription",
        cascade="all, delete-orphan", lazy="noload",
    )


class WebhookDelivery(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "webhook_v2_deliveries"
    __table_args__ = (
        Index("ix_wv2_deliv_subscription_id", "subscription_id"),
        Index("ix_wv2_deliv_status", "status"),
        Index("ix_wv2_deliv_created_at", "created_at"),
        Index("ix_wv2_deliv_event_type", "event_type"),
    )

    subscription_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("webhook_v2_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    subscription: Mapped[WebhookSubscription] = relationship(
        "WebhookSubscription", back_populates="deliveries",
    )


class WebhookDeadLetter(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "webhook_v2_dead_letters"
    __table_args__ = (
        Index("ix_wv2_dlq_created_at", "created_at"),
        Index("ix_wv2_dlq_delivery_id", "delivery_id"),
    )

    delivery_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("webhook_v2_deliveries.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    requeued: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
