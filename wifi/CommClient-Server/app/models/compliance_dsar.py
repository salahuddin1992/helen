"""
DSAR (GDPR Article 15) requests — Module AB.
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


VALID_DSAR_TYPES = ("access", "portability", "rectification")
VALID_DSAR_STATUSES = (
    "pending", "identity_verified", "running", "fulfilled",
    "rejected", "expired", "failed",
)


class DSARRequest(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "compliance_dsar_requests"
    __table_args__ = (
        Index("ix_cmp_dsar_status", "status"),
        Index("ix_cmp_dsar_subject_id", "subject_id"),
        Index("ix_cmp_dsar_deadline", "deadline_at"),
    )

    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    subject_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    request_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="access", server_default="access",
    )
    identity_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    identity_proof: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True,
    )

    scope: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending",
    )

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    response_letter: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system",
    )
