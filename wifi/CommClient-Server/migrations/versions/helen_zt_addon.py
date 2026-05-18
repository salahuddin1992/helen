"""Phase 7 / Module AL — Zero-Trust networking tables.

Adds six tables:
    zt_workload_identities
    zt_device_attestations
    zt_access_policies
    zt_access_requests
    zt_jit_grants
    zt_continuous_assessments

Revision ID: helen_zt_addon
Revises: helen_edge_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_zt_addon"
down_revision = "helen_edge_addon"
branch_labels = ("helen_zt_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "zt_workload_identities",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("spiffe_id", sa.String(length=255), nullable=False),
        sa.Column("workload_type", sa.String(length=32), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parent_identity_id", sa.String(length=32),
                  sa.ForeignKey("zt_workload_identities.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("spiffe_id", name="uq_zt_workload_spiffe"),
    )
    op.create_index("ix_zt_workload_kind", "zt_workload_identities", ["workload_type"])
    op.create_index("ix_zt_workload_parent", "zt_workload_identities", ["parent_identity_id"])
    op.create_index("ix_zt_workload_expires", "zt_workload_identities", ["expires_at"])

    op.create_table(
        "zt_device_attestations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("os", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("os_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("app_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("disk_encrypted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("screen_lock", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("antivirus_active", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("jailbroken", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("attested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("risk_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_zt_device_user", "zt_device_attestations", ["user_id"])
    op.create_index("ix_zt_device_device", "zt_device_attestations", ["device_id"])
    op.create_index("ix_zt_device_attested", "zt_device_attestations", ["attested_at"])
    op.create_index("ix_zt_device_valid", "zt_device_attestations", ["valid_until"])

    op.create_table(
        "zt_access_policies",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("subject_selector", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("resource_selector", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("allow", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("conditions", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("obligations", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_zt_policy_priority", "zt_access_policies", ["priority"])
    op.create_index("ix_zt_policy_enabled", "zt_access_policies", ["enabled"])

    op.create_table(
        "zt_access_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("requester_identity", sa.String(length=255), nullable=False),
        sa.Column("resource", sa.String(length=512), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("reasons", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("obligations", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("risk_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_zt_access_session", "zt_access_requests", ["session_id"])
    op.create_index("ix_zt_access_decided", "zt_access_requests", ["decided_at"])
    op.create_index("ix_zt_access_decision", "zt_access_requests", ["decision"])
    op.create_index("ix_zt_access_subject", "zt_access_requests", ["requester_identity"])

    op.create_table(
        "zt_jit_grants",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource", sa.String(length=512), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("granted_by", sa.String(length=32), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_zt_jit_user", "zt_jit_grants", ["user_id"])
    op.create_index("ix_zt_jit_expires", "zt_jit_grants", ["expires_at"])
    op.create_index("ix_zt_jit_status", "zt_jit_grants", ["status"])

    op.create_table(
        "zt_continuous_assessments",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("check_kind", sa.String(length=64), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_zt_assess_session", "zt_continuous_assessments", ["session_id"])
    op.create_index("ix_zt_assess_kind", "zt_continuous_assessments", ["check_kind"])
    op.create_index("ix_zt_assess_eval", "zt_continuous_assessments", ["evaluated_at"])


def downgrade() -> None:
    op.drop_table("zt_continuous_assessments")
    op.drop_table("zt_jit_grants")
    op.drop_table("zt_access_requests")
    op.drop_table("zt_access_policies")
    op.drop_table("zt_device_attestations")
    op.drop_table("zt_workload_identities")
