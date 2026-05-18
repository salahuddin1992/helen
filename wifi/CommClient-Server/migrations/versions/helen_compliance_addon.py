"""Phase 6 / Module AB — compliance tables.

Adds five tables:
    compliance_data_export_requests
    compliance_data_deletion_requests
    compliance_consent_records
    compliance_retention_policies
    compliance_pii_inventory

Revision ID: helen_compliance_addon
Revises: helen_dr_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_compliance_addon"
down_revision = "helen_dr_addon"
branch_labels = ("helen_compliance_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compliance_data_export_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("downloaded", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_cmp_export_user_id",
                    "compliance_data_export_requests", ["user_id"])
    op.create_index("ix_cmp_export_status",
                    "compliance_data_export_requests", ["status"])
    op.create_index("ix_cmp_export_requested_at",
                    "compliance_data_export_requests", ["requested_at"])

    op.create_table(
        "compliance_data_deletion_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dry_run_report", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("confirmation_token", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_cmp_delete_user_id",
                    "compliance_data_deletion_requests", ["user_id"])
    op.create_index("ix_cmp_delete_status",
                    "compliance_data_deletion_requests", ["status"])
    op.create_index("ix_cmp_delete_scheduled_for",
                    "compliance_data_deletion_requests", ["scheduled_for"])

    op.create_table(
        "compliance_consent_records",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("consent_type", sa.String(length=32), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("granted_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.String(length=32), nullable=False, server_default="1.0"),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
    )
    op.create_index("ix_cmp_consent_user_id",
                    "compliance_consent_records", ["user_id"])
    op.create_index("ix_cmp_consent_type",
                    "compliance_consent_records", ["consent_type"])
    op.create_index("ix_cmp_consent_granted_at",
                    "compliance_consent_records", ["granted_at"])

    op.create_table(
        "compliance_retention_policies",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("action", sa.String(length=16), nullable=False, server_default="delete"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_affected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("entity_type", name="uq_cmp_retention_entity_type"),
    )
    op.create_index("ix_cmp_retention_enabled",
                    "compliance_retention_policies", ["enabled"])

    op.create_table(
        "compliance_pii_inventory",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("table_name", sa.String(length=128), nullable=False),
        sa.Column("column_name", sa.String(length=128), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False,
                  server_default="none"),
        sa.Column("encryption_status", sa.String(length=16), nullable=False,
                  server_default="plain"),
        sa.Column("masking_rule", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("table_name", "column_name",
                            name="uq_cmp_pii_table_col"),
    )
    op.create_index("ix_cmp_pii_classification",
                    "compliance_pii_inventory", ["classification"])


def downgrade() -> None:
    op.drop_index("ix_cmp_pii_classification",
                  table_name="compliance_pii_inventory")
    op.drop_table("compliance_pii_inventory")

    op.drop_index("ix_cmp_retention_enabled",
                  table_name="compliance_retention_policies")
    op.drop_table("compliance_retention_policies")

    op.drop_index("ix_cmp_consent_granted_at",
                  table_name="compliance_consent_records")
    op.drop_index("ix_cmp_consent_type",
                  table_name="compliance_consent_records")
    op.drop_index("ix_cmp_consent_user_id",
                  table_name="compliance_consent_records")
    op.drop_table("compliance_consent_records")

    op.drop_index("ix_cmp_delete_scheduled_for",
                  table_name="compliance_data_deletion_requests")
    op.drop_index("ix_cmp_delete_status",
                  table_name="compliance_data_deletion_requests")
    op.drop_index("ix_cmp_delete_user_id",
                  table_name="compliance_data_deletion_requests")
    op.drop_table("compliance_data_deletion_requests")

    op.drop_index("ix_cmp_export_requested_at",
                  table_name="compliance_data_export_requests")
    op.drop_index("ix_cmp_export_status",
                  table_name="compliance_data_export_requests")
    op.drop_index("ix_cmp_export_user_id",
                  table_name="compliance_data_export_requests")
    op.drop_table("compliance_data_export_requests")
