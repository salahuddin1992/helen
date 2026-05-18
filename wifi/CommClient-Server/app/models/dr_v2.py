"""
Phase 6 / Module AA-v2 — Disaster Recovery v2 ORM models.

Extends the original ``app.models.dr`` package with a richer model surface
required by the Admin DR Console v2:

    dr_v2_destinations      (richer destination metadata + capacity)
    dr_v2_backups           (BackupRun with chunk manifest)
    dr_v2_backup_chunks     (per-chunk hash + offset)
    dr_v2_policies          (BackupPolicy — cron, scope, retention, hooks)
    dr_v2_jobs              (DRJob — backup + restore job tracking)
    dr_v2_drills            (DRDrill v2 — full drill report)
    dr_v2_keys              (DREncryptionKey — local + HSM backends)

The legacy tables (``dr_backup_jobs``, ``dr_backup_destinations``, ...)
are NOT touched.  The two systems coexist; new code uses these v2 tables,
the legacy backup_engine continues to write to the legacy tables until the
operator migrates.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


VALID_DR_V2_DESTINATION_KINDS = (
    "local-disk", "nfs", "smb", "sftp",
    "minio-s3-onprem", "tape-lto", "usb-removable",
)

VALID_DR_V2_BACKUP_STATUSES = (
    "pending", "running", "succeeded", "failed", "aborted", "archived",
)

VALID_DR_V2_JOB_TYPES = ("backup", "restore", "verify", "drill", "archive")
VALID_DR_V2_JOB_STATUSES = (
    "queued", "running", "succeeded", "failed", "cancelled",
)

VALID_DR_V2_POLICY_CADENCES = ("full", "incremental", "diff")
VALID_DR_V2_KEY_ALGOS = ("aes-256-gcm", "chacha20-poly1305")


# ── DRDestination ──────────────────────────────────────────────────────


class DRDestination(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """LAN-only DR destination with capacity + health metadata.

    The ``kind`` column is constrained to the LAN-only set; AWS / GCP /
    Azure are intentionally absent. The MinIO/S3 driver enforces an
    additional public-host blacklist (amazonaws.com, googleapis.com,
    blob.core.windows.net) at runtime.
    """

    __tablename__ = "dr_v2_destinations"
    __table_args__ = (
        UniqueConstraint("name", name="uq_dr_v2_destinations_name"),
        Index("ix_dr_v2_destinations_kind", "kind"),
        Index("ix_dr_v2_destinations_enabled", "enabled"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100",
    )
    capacity_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    used_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    last_health_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


# ── DRPolicy ──────────────────────────────────────────────────────


class DRPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Backup policy — scope, schedule, retention, hooks."""

    __tablename__ = "dr_v2_policies"
    __table_args__ = (
        UniqueConstraint("name", name="uq_dr_v2_policies_name"),
        Index("ix_dr_v2_policies_enabled", "enabled"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cron_schedule: Mapped[str] = mapped_column(
        String(64), nullable=False, default="0 2 * * *",
    )
    scope: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    cadence: Mapped[str] = mapped_column(
        String(16), nullable=False, default="full", server_default="full",
    )
    retention: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
        comment="GFS retention: {daily,weekly,monthly,yearly}",
    )
    encryption_key_ref: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    pre_hook: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_hook: Mapped[str | None] = mapped_column(Text, nullable=True)
    destinations: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list,
        comment="ordered list of destination IDs with priorities",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


# ── DRBackup + DRBackupChunk ──────────────────────────────────────────


class DRBackup(Base, UUIDPrimaryKeyMixin):
    """A single backup run.

    Distinct from the legacy ``dr_backup_jobs`` row: a v2 backup is the
    *logical* unit (full/incremental/diff), while DRJob tracks the
    *execution lifecycle* (queued → running → done).  A backup may be
    re-verified or re-archived many times, each producing a fresh job.
    """

    __tablename__ = "dr_v2_backups"
    __table_args__ = (
        Index("ix_dr_v2_backups_policy_id", "policy_id"),
        Index("ix_dr_v2_backups_destination_id", "destination_id"),
        Index("ix_dr_v2_backups_status", "status"),
        Index("ix_dr_v2_backups_started_at", "started_at"),
    )

    policy_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("dr_v2_policies.id", ondelete="SET NULL"),
        nullable=True,
    )
    destination_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("dr_v2_destinations.id", ondelete="SET NULL"),
        nullable=True,
    )
    base_backup_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("dr_v2_backups.id", ondelete="SET NULL"),
        nullable=True,
        comment="for incremental/diff: parent full backup",
    )
    cadence: Mapped[str] = mapped_column(
        String(16), nullable=False, default="full", server_default="full",
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
        BigInteger, nullable=False, default=0, server_default="0",
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    sha256_root: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    encrypted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    encryption_key_ref: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_verify_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    chunks: Mapped[list["DRBackupChunk"]] = relationship(
        "DRBackupChunk", back_populates="backup",
        cascade="all, delete-orphan", lazy="noload",
    )


class DRBackupChunk(Base, UUIDPrimaryKeyMixin):
    """Per-chunk metadata for a backup run."""

    __tablename__ = "dr_v2_backup_chunks"
    __table_args__ = (
        Index("ix_dr_v2_backup_chunks_backup_id", "backup_id"),
        Index("ix_dr_v2_backup_chunks_seq", "backup_id", "seq"),
    )

    backup_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("dr_v2_backups.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_size: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0,
    )
    nonce_hex: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )

    backup: Mapped["DRBackup"] = relationship(
        "DRBackup", back_populates="chunks",
    )


# ── DRJob ──────────────────────────────────────────────────────


class DRJob(Base, UUIDPrimaryKeyMixin):
    """Generic job tracking (backup, restore, verify, drill, archive)."""

    __tablename__ = "dr_v2_jobs"
    __table_args__ = (
        Index("ix_dr_v2_jobs_kind", "kind"),
        Index("ix_dr_v2_jobs_status", "status"),
        Index("ix_dr_v2_jobs_created_at", "created_at"),
        Index("ix_dr_v2_jobs_backup_id", "backup_id"),
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued",
    )
    backup_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    policy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    destination_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    progress_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


# ── DRDrillV2 ──────────────────────────────────────────────────────


class DRDrillV2(Base, UUIDPrimaryKeyMixin):
    """Periodic DR drill v2 — richer report and scheduling."""

    __tablename__ = "dr_v2_drills"
    __table_args__ = (
        Index("ix_dr_v2_drills_status", "status"),
        Index("ix_dr_v2_drills_scheduled_at", "scheduled_at"),
    )

    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="scheduled", server_default="scheduled",
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default="sandbox", server_default="sandbox",
    )
    rto_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    rpo_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    integrity_ok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    steps: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    recommendations: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    report: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ── DREncryptionKey ──────────────────────────────────────────────────────


class DREncryptionKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """DR-specific encryption key with HSM-passthrough metadata."""

    __tablename__ = "dr_v2_keys"
    __table_args__ = (
        UniqueConstraint("alias", name="uq_dr_v2_keys_alias"),
        Index("ix_dr_v2_keys_active", "active"),
    )

    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, default="aes-256-gcm",
    )
    public_blob: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="base64 — only the public half / fingerprint",
    )
    encrypted_material_ref: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="opaque pointer to where the wrapped DEK lives",
    )
    backend: Mapped[str] = mapped_column(
        String(32), nullable=False, default="local", server_default="local",
        comment="local | hsm | yubikey",
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1",
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    rotates_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
