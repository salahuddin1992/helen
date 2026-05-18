"""
Phase 6 / Module AB — Compliance & Privacy Pack models.

Five tables:
    compliance_data_export_requests
    compliance_data_deletion_requests
    compliance_consent_records
    compliance_retention_policies
    compliance_pii_inventory
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_EXPORT_STATUSES = ("pending", "running", "ready", "failed", "expired")
VALID_DELETION_STATUSES = ("pending", "scheduled", "running", "completed", "failed", "cancelled")
VALID_CONSENT_TYPES = ("privacy", "marketing", "cookies", "tos", "data_processing")
VALID_RETENTION_ACTIONS = ("delete", "anonymize")
VALID_PII_CLASSIFICATIONS = ("pii", "phi", "financial", "credentials", "none")


class DataExportRequest(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "compliance_data_export_requests"
    __table_args__ = (
        Index("ix_cmp_export_user_id", "user_id"),
        Index("ix_cmp_export_status", "status"),
        Index("ix_cmp_export_requested_at", "requested_at"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    downloaded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class DataDeletionRequest(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "compliance_data_deletion_requests"
    __table_args__ = (
        Index("ix_cmp_delete_user_id", "user_id"),
        Index("ix_cmp_delete_status", "status"),
        Index("ix_cmp_delete_scheduled_for", "scheduled_for"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    dry_run_report: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    confirmation_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConsentRecord(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "compliance_consent_records"
    __table_args__ = (
        Index("ix_cmp_consent_user_id", "user_id"),
        Index("ix_cmp_consent_type", "consent_type"),
        Index("ix_cmp_consent_granted_at", "granted_at"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    consent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    granted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1.0", server_default="1.0",
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)


class RetentionPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "compliance_retention_policies"
    __table_args__ = (
        UniqueConstraint("entity_type", name="uq_cmp_retention_entity_type"),
        Index("ix_cmp_retention_enabled", "enabled"),
    )

    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=365, server_default="365",
    )
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="delete", server_default="delete",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_run_affected: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )


class PIIInventoryEntry(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "compliance_pii_inventory"
    __table_args__ = (
        UniqueConstraint(
            "table_name", "column_name", name="uq_cmp_pii_table_col",
        ),
        Index("ix_cmp_pii_classification", "classification"),
    )

    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    column_name: Mapped[str] = mapped_column(String(128), nullable=False)
    classification: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none",
    )
    encryption_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="plain", server_default="plain",
    )
    masking_rule: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
