"""Phase 6 / Module AA — Disaster Recovery tables.

Adds five tables:
    dr_backup_jobs
    dr_backup_destinations
    dr_restore_points
    dr_restore_operations
    dr_drills

Revision ID: helen_dr_addon
Revises: helen_ai_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_dr_addon"
down_revision = "helen_ai_addon"
branch_labels = ("helen_dr_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dr_backup_destinations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False,
                  server_default="local"),
        sa.Column("config", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("last_used", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_dr_backup_destinations_name"),
    )
    op.create_index("ix_dr_backup_destinations_enabled",
                    "dr_backup_destinations", ["enabled"])

    op.create_table(
        "dr_backup_jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False,
                  server_default="full"),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("destination_id", sa.String(length=32),
                  sa.ForeignKey("dr_backup_destinations.id",
                                ondelete="SET NULL"),
                  nullable=True),
        sa.Column("destination", sa.String(length=512), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("encrypted", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("encrypted_key_ref", sa.String(length=128), nullable=True),
        sa.Column("base_job_id", sa.String(length=32),
                  sa.ForeignKey("dr_backup_jobs.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_dr_backup_jobs_kind", "dr_backup_jobs", ["kind"])
    op.create_index("ix_dr_backup_jobs_status", "dr_backup_jobs", ["status"])
    op.create_index("ix_dr_backup_jobs_started_at",
                    "dr_backup_jobs", ["started_at"])
    op.create_index("ix_dr_backup_jobs_destination",
                    "dr_backup_jobs", ["destination_id"])

    op.create_table(
        "dr_restore_points",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("backup_job_id", sa.String(length=32),
                  sa.ForeignKey("dr_backup_jobs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False,
                  server_default="0"),
        sa.Column("app_version", sa.String(length=64), nullable=False,
                  server_default="0"),
        sa.Column("manifest", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_dr_restore_points_backup_job_id",
                    "dr_restore_points", ["backup_job_id"])
    op.create_index("ix_dr_restore_points_created_at",
                    "dr_restore_points", ["created_at"])

    op.create_table(
        "dr_restore_operations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("restore_point_id", sa.String(length=32),
                  sa.ForeignKey("dr_restore_points.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("initiated_by", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("confirmation_token", sa.String(length=64), nullable=True),
        sa.Column("report", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_dr_restore_operations_restore_point_id",
                    "dr_restore_operations", ["restore_point_id"])
    op.create_index("ix_dr_restore_operations_status",
                    "dr_restore_operations", ["status"])
    op.create_index("ix_dr_restore_operations_started_at",
                    "dr_restore_operations", ["started_at"])

    op.create_table(
        "dr_drills",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("rto_seconds", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("rpo_seconds", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("report", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
    )
    op.create_index("ix_dr_drills_scheduled_at", "dr_drills", ["scheduled_at"])
    op.create_index("ix_dr_drills_executed_at", "dr_drills", ["executed_at"])


def downgrade() -> None:
    op.drop_index("ix_dr_drills_executed_at", table_name="dr_drills")
    op.drop_index("ix_dr_drills_scheduled_at", table_name="dr_drills")
    op.drop_table("dr_drills")

    op.drop_index("ix_dr_restore_operations_started_at",
                  table_name="dr_restore_operations")
    op.drop_index("ix_dr_restore_operations_status",
                  table_name="dr_restore_operations")
    op.drop_index("ix_dr_restore_operations_restore_point_id",
                  table_name="dr_restore_operations")
    op.drop_table("dr_restore_operations")

    op.drop_index("ix_dr_restore_points_created_at",
                  table_name="dr_restore_points")
    op.drop_index("ix_dr_restore_points_backup_job_id",
                  table_name="dr_restore_points")
    op.drop_table("dr_restore_points")

    op.drop_index("ix_dr_backup_jobs_destination",
                  table_name="dr_backup_jobs")
    op.drop_index("ix_dr_backup_jobs_started_at",
                  table_name="dr_backup_jobs")
    op.drop_index("ix_dr_backup_jobs_status", table_name="dr_backup_jobs")
    op.drop_index("ix_dr_backup_jobs_kind", table_name="dr_backup_jobs")
    op.drop_table("dr_backup_jobs")

    op.drop_index("ix_dr_backup_destinations_enabled",
                  table_name="dr_backup_destinations")
    op.drop_table("dr_backup_destinations")
