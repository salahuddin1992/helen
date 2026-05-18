"""
Audit SIEM — Legal Hold model.

A legal hold suspends retention/deletion of a slice of audit data
(or related resources) for the duration of an investigation,
litigation, or regulatory inquiry. Once placed, every retention
policy must skip resources covered by an active hold.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_HOLD_STATUSES = ("active", "released", "expired")


class LegalHold(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_legal_holds"
    __table_args__ = (
        Index("ix_audit_legal_holds_status", "status"),
        Index("ix_audit_legal_holds_starts_at", "starts_at"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    case_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active",
    )

    # Scope JSON: {actors: [...], channels: [...], resources: [...],
    #              keywords: [...], file_types: [...], severity_min: "..."}
    scope: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )

    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    released_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "case_ref": self.case_ref,
            "description": self.description,
            "status": self.status,
            "scope": dict(self.scope or {}),
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "released_at": self.released_at.isoformat() if self.released_at else None,
            "release_reason": self.release_reason,
            "created_by": self.created_by,
            "released_by": self.released_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
