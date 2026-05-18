"""
Advanced retention policies — Module AB.

This extends the existing simple ``RetentionPolicy`` (entity_type / days /
action) with a richer per-resource policy model that supports:

* archive, delete, anonymize, redact_pii actions
* free-form selector JSON (per-channel / per-tenant / per-classification)
* dry-run preview counts
* job tracking

Existing ``app.models.compliance.RetentionPolicy`` is preserved untouched
to avoid breakage in legacy paths. This model lives in a new table.
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


VALID_ADV_RETENTION_ACTIONS = (
    "delete", "anonymize", "archive", "redact_pii",
)


class ComplianceRetentionPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "compliance_retention_policies_v2"
    __table_args__ = (
        Index("ix_cmp_retv2_enabled", "enabled"),
        Index("ix_cmp_retv2_resource_type", "resource_type"),
    )

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # selector JSON: {custodians[], channels[], tenants[], classifications[]}
    selector: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )

    retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=365, server_default="365",
    )
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="delete", server_default="delete",
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    respect_legal_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )

    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_run_affected: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_run_skipped_held: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    created_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system", server_default="system",
    )


class ComplianceRetentionJob(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "compliance_retention_jobs"
    __table_args__ = (
        Index("ix_cmp_retjob_status", "status"),
        Index("ix_cmp_retjob_started_at", "started_at"),
    )

    policy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system",
    )
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    affected: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    skipped_held: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
