"""Tenancy + RBAC + Billing Portal addon — license signing, plan audit,
admin impersonation sessions, password resets.

Adds five tables:

    billing_licenses              — Ed25519-signed offline license blobs
    billing_license_revocations   — CRL of revoked license keys
    billing_plan_audit            — change history for plan upserts/removals
    tenant_admin_sessions         — short-lived impersonation tokens
    rbac_user_password_resets     — admin-initiated temporary passwords

Revision ID: helen_tenancy_portal_addon
Revises: helen_plugins_marketplace_addon (was helen_billing_addon)
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_tenancy_portal_addon"
# Re-pointed off helen_plugins_marketplace_addon (which now sits above
# helen_analytics_addon) to keep one linear chain. Original down was
# helen_billing_addon (workspaces ancestor preserved through the chain).
down_revision = "helen_plugins_marketplace_addon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── billing_licenses ─────────────────────────────────────────
    op.create_table(
        "billing_licenses",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("license_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("plan_slug", sa.String(length=64), nullable=False),
        sa.Column("seats", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("features", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("payload_json", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("signature_b64", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=32), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="active"),
        sa.Column("issued_by", sa.String(length=32), nullable=True),
        sa.Column("license_metadata", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_billing_licenses_workspace_id",
                    "billing_licenses", ["workspace_id"])
    op.create_index("ix_billing_licenses_status",
                    "billing_licenses", ["status"])
    op.create_index("ix_billing_licenses_expires_at",
                    "billing_licenses", ["expires_at"])

    # ── billing_license_revocations ──────────────────────────────
    op.create_table(
        "billing_license_revocations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("license_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("workspace_id", sa.String(length=32), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("revoked_by", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_billing_revocations_revoked_at",
                    "billing_license_revocations", ["revoked_at"])

    # ── billing_plan_audit ───────────────────────────────────────
    op.create_table(
        "billing_plan_audit",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("plan_slug", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("ix_billing_plan_audit_slug",
                    "billing_plan_audit", ["plan_slug"])
    op.create_index("ix_billing_plan_audit_actor",
                    "billing_plan_audit", ["actor_id"])
    op.create_index("ix_billing_plan_audit_at",
                    "billing_plan_audit", ["occurred_at"])

    # ── tenant_admin_sessions ────────────────────────────────────
    op.create_table(
        "tenant_admin_sessions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("token", sa.String(length=96), nullable=False, unique=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("issued_by", sa.String(length=32), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("ix_tenant_admin_sessions_workspace_id",
                    "tenant_admin_sessions", ["workspace_id"])
    op.create_index("ix_tenant_admin_sessions_expires_at",
                    "tenant_admin_sessions", ["expires_at"])

    # ── rbac_user_password_resets ────────────────────────────────
    op.create_table(
        "rbac_user_password_resets",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("temp_password_hash", sa.String(length=256), nullable=False),
        sa.Column("issued_by", sa.String(length=32), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_rbac_pw_resets_user_id",
                    "rbac_user_password_resets", ["user_id"])
    op.create_index("ix_rbac_pw_resets_expires_at",
                    "rbac_user_password_resets", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_rbac_pw_resets_expires_at",
                  table_name="rbac_user_password_resets")
    op.drop_index("ix_rbac_pw_resets_user_id",
                  table_name="rbac_user_password_resets")
    op.drop_table("rbac_user_password_resets")

    op.drop_index("ix_tenant_admin_sessions_expires_at",
                  table_name="tenant_admin_sessions")
    op.drop_index("ix_tenant_admin_sessions_workspace_id",
                  table_name="tenant_admin_sessions")
    op.drop_table("tenant_admin_sessions")

    op.drop_index("ix_billing_plan_audit_at", table_name="billing_plan_audit")
    op.drop_index("ix_billing_plan_audit_actor", table_name="billing_plan_audit")
    op.drop_index("ix_billing_plan_audit_slug", table_name="billing_plan_audit")
    op.drop_table("billing_plan_audit")

    op.drop_index("ix_billing_revocations_revoked_at",
                  table_name="billing_license_revocations")
    op.drop_index("ix_billing_licenses_expires_at",
                  table_name="billing_licenses")
    op.drop_index("ix_billing_licenses_status", table_name="billing_licenses")
    op.drop_index("ix_billing_licenses_workspace_id",
                  table_name="billing_licenses")
    op.drop_table("billing_licenses")
