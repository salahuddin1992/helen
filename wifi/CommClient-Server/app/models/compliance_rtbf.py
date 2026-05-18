"""
RTBF (GDPR Article 17 — Right To Be Forgotten) requests — Module AB.

Execution rules:
* Holds short-circuit: if any active hold covers the subject, the
  request is blocked (Art. 17(3)(e)).
* Audit-chain entries are NEVER deleted — they are marked redacted.
* Messages: content -> "[redacted]" but timestamps + hash preserved.
* Files: deleted from object store.
* Profile: deleted; users.id retained as tombstone for foreign keys.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_RTBF_STATUSES = (
    "pending", "blocked", "approved", "running", "completed",
    "failed", "cancelled",
)


class RTBFRequest(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "compliance_rtbf_requests"
    __table_args__ = (
        Index("ix_cmp_rtbf_status", "status"),
        Index("ix_cmp_rtbf_subject_id", "subject_id"),
    )

    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)

    scope: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )

    # Holds detected at create-time
    hold_conflicts: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Counters from execution
    messages_redacted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    files_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    audit_entries_marked: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    verification_report: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system",
    )
