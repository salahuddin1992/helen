"""DR v2 Console addon — chunked backups, policies, drills v2, key registry.

Adds seven tables:

    dr_v2_destinations
    dr_v2_policies
    dr_v2_backups
    dr_v2_backup_chunks
    dr_v2_jobs
    dr_v2_drills
    dr_v2_keys

Revision ID: helen_dr_v2_addon
Revises: helen_dr_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_dr_v2_addon"
# Re-pointed off helen_compliance_workbench_addon to keep the chain
# linear. Original ancestor helen_dr_addon is preserved via depends_on.
down_revision = "helen_compliance_workbench_addon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── dr_v2_destinations ───────────────────────────────────────
    op.create_table(
        "dr_v2_destinations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("capacity_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_health_ok", sa.Boolean(), nullable=True),
        sa.Column("last_latency_ms", sa.Float(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=2048), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_dr_v2_destinations_name"),
    )
    op.create_index("ix_dr_v2_destinations_kind", "dr_v2_destinations", ["kind"])
    op.create_index("ix_dr_v2_destinations_enabled", "dr_v2_destinations", ["enabled"])

    # ── dr_v2_policies ───────────────────────────────────────────
    op.create_table(
        "dr_v2_policies",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("cron_schedule", sa.String(length=64), nullable=False,
                  server_default="0 2 * * *"),
        sa.Column("scope", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("cadence", sa.String(length=16), nullable=False, server_default="full"),
        sa.Column("retention", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("encryption_key_ref", sa.String(length=64), nullable=True),
        sa.Column("pre_hook", sa.Text(), nullable=True),
        sa.Column("post_hook", sa.Text(), nullable=True),
        sa.Column("destinations", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_dr_v2_policies_name"),
    )
    op.create_index("ix_dr_v2_policies_enabled", "dr_v2_policies", ["enabled"])

    # ── dr_v2_backups ────────────────────────────────────────────
    op.create_table(
        "dr_v2_backups",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("policy_id", sa.String(length=32),
                  sa.ForeignKey("dr_v2_policies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("destination_id", sa.String(length=32),
                  sa.ForeignKey("dr_v2_destinations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("base_backup_id", sa.String(length=32),
                  sa.ForeignKey("dr_v2_backups.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cadence", sa.String(length=16), nullable=False, server_default="full"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256_root", sa.String(length=64), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("encrypted", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("encryption_key_ref", sa.String(length=64), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verify_ok", sa.Boolean(), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_dr_v2_backups_policy_id", "dr_v2_backups", ["policy_id"])
    op.create_index("ix_dr_v2_backups_destination_id", "dr_v2_backups", ["destination_id"])
    op.create_index("ix_dr_v2_backups_status", "dr_v2_backups", ["status"])
    op.create_index("ix_dr_v2_backups_started_at", "dr_v2_backups", ["started_at"])

    # ── dr_v2_backup_chunks ─────────────────────────────────────
    op.create_table(
        "dr_v2_backup_chunks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("backup_id", sa.String(length=32),
                  sa.ForeignKey("dr_v2_backups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("encrypted_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("nonce_hex", sa.String(length=64), nullable=True),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_dr_v2_backup_chunks_backup_id", "dr_v2_backup_chunks", ["backup_id"])
    op.create_index("ix_dr_v2_backup_chunks_seq", "dr_v2_backup_chunks", ["backup_id", "seq"])

    # ── dr_v2_jobs ──────────────────────────────────────────────
    op.create_table(
        "dr_v2_jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("backup_id", sa.String(length=32), nullable=True),
        sa.Column("policy_id", sa.String(length=32), nullable=True),
        sa.Column("destination_id", sa.String(length=32), nullable=True),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_message", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("result", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_dr_v2_jobs_kind", "dr_v2_jobs", ["kind"])
    op.create_index("ix_dr_v2_jobs_status", "dr_v2_jobs", ["status"])
    op.create_index("ix_dr_v2_jobs_created_at", "dr_v2_jobs", ["created_at"])
    op.create_index("ix_dr_v2_jobs_backup_id", "dr_v2_jobs", ["backup_id"])

    # ── dr_v2_drills ───────────────────────────────────────────
    op.create_table(
        "dr_v2_drills",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="scheduled"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope", sa.String(length=32), nullable=False, server_default="sandbox"),
        sa.Column("rto_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rpo_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("integrity_ok", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("steps", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("recommendations", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("report", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_dr_v2_drills_status", "dr_v2_drills", ["status"])
    op.create_index("ix_dr_v2_drills_scheduled_at", "dr_v2_drills", ["scheduled_at"])

    # ── dr_v2_keys ─────────────────────────────────────────────
    op.create_table(
        "dr_v2_keys",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("alias", sa.String(length=128), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False,
                  server_default="aes-256-gcm"),
        sa.Column("public_blob", sa.Text(), nullable=True),
        sa.Column("encrypted_material_ref", sa.String(length=512), nullable=True),
        sa.Column("backend", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotates_from", sa.String(length=32), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("alias", name="uq_dr_v2_keys_alias"),
    )
    op.create_index("ix_dr_v2_keys_active", "dr_v2_keys", ["active"])


def downgrade() -> None:
    op.drop_table("dr_v2_keys")
    op.drop_table("dr_v2_drills")
    op.drop