"""
Federation operational event log (DISTINCT from the protocol DAG).

``FederationEvent`` (federation_v2) tracks the signed event DAG that is
part of the wire protocol. THIS log is purely operational — every
admin action, sync milestone, conflict, partition, role-change, etc.,
goes here so the Health-Map UI can render a live timeline.

Event categories
----------------
    handshake        — initial / re-handshake completed / failed
    sync             — table sync started / completed / lag exceeded
    conflict         — DAG conflict surfaced and resolved
    partition        — peer became unreachable / recovered
    role_change      — master ↔ follower transitions
    shaper_change    — bandwidth rule change
    cert             — cert issued / rotated / expiring
    quorum           — election started / leader elected / split-brain
    admin            — admin destructive action (audit)
    diagnostic       — diagnose run with summary
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_FED_EVENT_CATEGORIES = (
    "handshake", "sync", "conflict", "partition",
    "role_change", "shaper_change", "cert", "quorum",
    "admin", "diagnostic", "policy",
)


class FederationEventLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "federation_event_log"
    __table_args__ = (
        Index("ix_fed_event_server", "server_id"),
        Index("ix_fed_event_category", "category"),
        Index("ix_fed_event_occurred", "occurred_at"),
        Index("ix_fed_event_severity", "severity"),
    )

    server_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="info", server_default="info",
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    actor: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FedEventLog {self.category}/{self.severity} server={self.server_id}>"
