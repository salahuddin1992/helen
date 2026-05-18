"""
Compliance Legal Holds — Module AB (Compliance / eDiscovery Workbench).

Tables:
    compliance_holds            — active and released legal holds
    compliance_hold_audit       — per-hold audit trail (release events,
                                  scope edits, custodian notifications)

A legal hold suspends retention policy actions on any resource whose
attributes match its ``scope`` JSON. Scope keys:

    custodians:     list[str]  — user_ids
    channels:       list[str]  — channel_ids
    date_range:     {start, end}   — ISO timestamps
    keywords:       list[str]
    file_types:     list[str]  — extensions
    message_types:  list[str]  — text/voice/file/poll/...

Holds are evaluated by ComplianceLegalHoldsService.is_under_hold().
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
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_HOLD_STATUSES = ("active", "released", "expired")


class ComplianceHold(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "compliance_holds"
    __table_args__ = (
        Index("ix_cmp_hold_status", "status"),
        Index("ix_cmp_hold_case_ref", "case_ref"),
        Index("ix_cmp_hold_created_by", "created_by"),
    )

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    case_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSON scope: see module docstring
    scope: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )

    retention_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    notify: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active",
    )
    created_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system", server_default="system",
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    released_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ComplianceHoldAudit(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "compliance_hold_audit"
    __table_args__ = (
        Index("ix_cmp_hold_audit_hold_id", "hold_id"),
        Index("ix_cmp_hold_audit_occurred_at", "occurred_at"),
    )

    hold_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("compliance_holds.id", ondelete="CASCADE"),
        nullable=False,
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system",
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
