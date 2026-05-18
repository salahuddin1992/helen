"""
Audit SIEM — Retention Policy model.

Retention policies describe how long audit (or related resource) data
is kept before it is archived, anonymised, or deleted. Active legal
holds always override retention.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_RETENTION_ACTIONS = ("archive", "delete", "anonymize")


class RetentionPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_retention_policies"
    __table_args__ = (
        Index("ix_audit_retention_resource", "resource_type"),
        Index("ix_audit_retention_enabled", "enabled"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="archive", server_default="archive",
    )

    # exemptions: {holds: bool, classifications: ["pii", "phi", ...]}
    exemptions: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )

    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_affected: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "resource_type": self.resource_type,
            "period_days": int(self.period_days or 0),
            "action": self.action,
            "exemptions": dict(self.exemptions or {}),
            "enabled": bool(self.enabled),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_affected": int(self.last_affected or 0),
            "created_by": self.created_by,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
