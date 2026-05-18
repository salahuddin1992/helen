"""
Audit log model — persistent security event trail.

Stores forensic-quality records of authentication, authorization,
admin actions, and security incidents for later querying via the
admin REST endpoint.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now


class AuditLog(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "audit_logs"

    # Event classification ─────────────────────────
    event: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
    )  # e.g. "auth.login", "authz.denied", "admin.user_banned"

    # Actor (may be anonymous for failed login attempts) ──
    user_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="anonymous", index=True,
    )
    ip_address: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unknown",
    )

    # Outcome ──────────────────────────────────────
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True,
    )

    # Structured details (JSON-encoded for portability across SQLite/Postgres) ─
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # When it happened ─────────────────────────────
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )

    __table_args__ = (
        Index("ix_audit_logs_user_event_time", "user_id", "event", "occurred_at"),
    )

    def __repr__(self) -> str:
        marker = "ok" if self.success else "FAIL"
        return f"<AuditLog {self.event} {marker} user={self.user_id[:8]}>"

    def to_dict(self) -> dict:
        import json

        details: dict | None = None
        if self.details_json:
            try:
                details = json.loads(self.details_json)
            except (ValueError, TypeError):
                details = {"_raw": self.details_json}

        return {
            "id": self.id,
            "event": self.event,
            "user_id": self.user_id,
            "ip_address": self.ip_address,
            "success": self.success,
            "details": details,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
        }
