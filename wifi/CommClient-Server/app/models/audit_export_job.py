"""
Audit SIEM — Export Job model.

Each row tracks an async export task: scope (filters), format,
progress, signed bundle path, signature, status, and audit metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_EXPORT_STATUSES = (
    "queued", "running", "ready", "failed", "cancelled", "expired",
)
VALID_EXPORT_FORMATS = (
    "jsonl", "jsonl-signed", "csv", "pdf", "zip-verifier",
)


class AuditExportJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_export_jobs"
    __table_args__ = (
        Index("ix_audit_export_status", "status"),
        Index("ix_audit_export_actor", "actor_id"),
    )

    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    format: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued",
    )
    progress: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    rows_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    rows_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_size: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hmac_signature: Mapped[str | None] = mapped_column(String(128), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "actor_id": self.actor_id,
            "scope": dict(self.scope or {}),
            "filters": dict(self.filters or {}),
            "format": self.format,
            "status": self.status,
            "progress": int(self.progress or 0),
            "rows_total": int(self.rows_total or 0),
            "rows_processed": int(self.rows_processed or 0),
            "file_path": self.file_path,
            "file_size": int(self.file_size or 0),
            "sha256": self.sha256,
            "hmac_signature": self.hmac_signature,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
