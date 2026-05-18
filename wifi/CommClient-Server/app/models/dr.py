"""
Phase 6 / Module AA — Disaster Recovery models.

Five tables that together describe the lifecycle of a backup:

    backup_jobs          — every full / incremental / snapshot run
    backup_destinations  — where a job can be uploaded (local, S3, SFTP, …)
    restore_points       — a verified, indexed pointer into a backup job
    restore_operations   — every restore attempt (dry-run or apply)
    dr_drills            — periodic end-to-end recovery rehearsals

These tables are append-only by convention; rows are never updated in place
except for the "status" / "completed_at" columns on long-running jobs.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_BACKUP_KINDS = ("full", "incremental", "snapshot")
VALID_BACKUP_STATUSES = ("pending", "running", "succeeded", "failed", "aborted")
VALID_DESTINATION_KINDS = ("local", "s3", "sftp", "azure_blob", "gcs")
VALID_RESTORE_STATUSES = (
    "pending", "verifying", "restoring", "succeeded", "failed", "aborted",
)


# ─────────────────────────────────────────────────────────────────────
# BackupJob
# ─────────────────────────────────────────────────────────────────────


class BackupJob(Base, UUIDPrimaryKeyMixin):
    """A single backup run — full / incremental / snapshot."""

    __tablename__ = "dr_backup_jobs"
    __table_args__ = (
        Index("ix_dr_backup_jobs_kind", "kind"),
        Index("ix_dr_backup_jobs_status", "status"),
        Index("ix_dr_backup_jobs_started_at", "started_at"),
        Index("ix_dr_backup_jobs_destination", "destination_id"),
    )

    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="full",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    destination_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("dr_backup_destinations.id", ondelete="SET NULL"),
        nullable=True,
    )
    destination: Mapped[str | None] = mapped_column(String(512), nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    encrypted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    encrypted_key_ref: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="opaque secret_store key id used to decrypt this archive",
    )
    base_job_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("dr_backup_jobs.id", ondelete="SET NULL"),
        nullable=True,
        comment="for incrementals: pointer to the parent full backup",
    )
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    restore_points: Mapped[list["RestorePoint"]] = relationship(
        "RestorePoint", back_populates="backup_job",
        cascade="all, delete-orphan", lazy="noload",
    )


# ─────────────────────────────────────────────────────────────────────
# BackupDestination
# ─────────────────────────────────────────────────────────────────────


class BackupDestination(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A pluggable upload target: local, S3, SFTP, Azure Blob, GCS."""

    __tablename__ = "dr_backup_destinations"
    __table_args__ = (
        UniqueConstraint("name", name="uq_dr_backup_destinations_name"),
        Index("ix_dr_backup_destinations_enabled", "enabled"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="local",
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
        comment="provider-specific config (bucket, prefix, host, port, …)",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    last_used: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)


# ─────────────────────────────────────────────────────────────────────
# RestorePoint
# ─────────────────────────────────────────────────────────────────────


class RestorePoint(Base, UUIDPrimaryKeyMixin):
    """A verified pointer into a `BackupJob` ready for restore."""

    __tablename__ = "dr_restore_points"
    __table_args__ = (
        Index("ix_dr_restore_points_backup_job_id", "backup_job_id"),
        Index("ix_dr_restore_points_created_at", "created_at"),
    )

    backup_job_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("dr_backup_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    app_version: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )

    backup_job: Mapped["BackupJob"] = relationship(
        "BackupJob", back_populates="restore_points",
    )
    operations: Mapped[list["RestoreOperation"]] = relationship(
        "RestoreOperation", back_populates="restore_point",
        cascade="all, delete-orphan", lazy="noload",
    )


# ─────────────────────────────────────────────────────────────────────
# RestoreOperation
# ─────────────────────────────────────────────────────────────────────


class RestoreOperation(Base, UUIDPrimaryKeyMixin):
    """A single restore attempt (dry-run or apply) against a RestorePoint."""

    __tablename__ = "dr_restore_operations"
    __table_args__ = (
        Index("ix_dr_restore_operations_restore_point_id", "restore_point_id"),
        Index("ix_dr_restore_operations_status", "status"),
        Index("ix_dr_restore_operations_started_at", "started_at"),
    )

    restore_point_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("dr_restore_points.id", ondelete="CASCADE"),
        nullable=False,
    )
    initiated_by: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    confirmation_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    report: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    restore_point: Mapped["RestorePoint"] = relationship(
        "RestorePoint", back_populates="operations",
    )


# ─────────────────────────────────────────────────────────────────────
# DRDrill
# ─────────────────────────────────────────────────────────────────────


class DRDrill(Base, UUIDPrimaryKeyMixin):
    """Periodic disaster-recovery rehearsal."""

    __tablename__ = "dr_drills"
    __table_args__ = (
        Index("ix_dr_drills_scheduled_at", "scheduled_at"),
        Index("ix_dr_drills_executed_at", "executed_at"),
    )

    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    rto_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="Recovery Time Objective — how long restore actually took",
    )
    rpo_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="Recovery Point Objective — staleness of recovered data",
    )
    report: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
