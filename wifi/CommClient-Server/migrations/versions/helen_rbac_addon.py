"""Phase 2 / Module G — RBAC addon.

Adds four tables for the granular RBAC system:
    rbac_roles
    rbac_permissions
    rbac_role_permissions
    rbac_user_roles

The legacy ``users.role`` column is intentionally untouched — the new
system runs alongside it. ``app.services.rbac.enforcer`` unions the
permissions implied by the legacy column with those granted through the
new tables.

Revision ID: helen_rbac_addon
Revises: 009
Create Date: 2026-05-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_rbac_addon"
down_revision = "009"
branch_labels = ("helen_rbac_addon",)
depends_on = None


def upgrade() -> None:
    # ── rbac_roles ───────────────────────────────────────────
    op.create_table(
        "rbac_roles",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_rbac_roles_name", "rbac_roles", ["name"], unique=True)

    # ── rbac_permissions ─────────────────────────────────────
    op.create_table(
        "rbac_permissions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("key", sa.String(length=96), nullable=False),
        sa.Column("category", sa.String(length=48), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
    )
    op.create_index("ix_rbac_permissions_key", "rbac_permissions",
                    ["key"], unique=True)
    op.create_index("ix_rbac_permissions_category", "rbac_permissions",
                    ["category"])

    # ── rbac_role_permissions ────────────────────────────────
    op.create_table(
        "rbac_role_permissions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("role_id", sa.String(length=32),
                  sa.ForeignKey("rbac_roles.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("permission_id", sa.String(length=32),
                  sa.ForeignKey("rbac_permissions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.UniqueConstraint("role_id", "permission_id",
                            name="uq_rbac_role_permission"),
    )
    op.create_index("ix_rbac_rp_role", "rbac_role_permissions", ["role_id"])
    op.create_index("ix_rbac_rp_perm", "rbac_role_permissions", ["permission_id"])

    # ── rbac_user_roles ──────────────────────────────────────
    op.create_table(
        "rbac_user_roles",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("role_id", sa.String(length=32),
                  sa.ForeignKey("rbac_roles.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("assigned_by", sa.String(length=32), nullable=True),
        sa.UniqueConstraint("user_id", "role_id", name="uq_rbac_user_role"),
    )
    op.create_index("ix_rbac_ur_user", "rbac_user_roles", ["user_id"])
    op.create_index("ix_rbac_ur_role", "rbac_user_roles", ["role_id"])


def downgrade() -> None:
    op.drop_index("ix_rbac_ur_role", table_name="rbac_user_roles")
    op.drop_index("ix_rbac_ur_user", table_name="rbac_user_roles")
    op.drop_table("rbac_user_roles")

    op.drop_index("ix_rbac_rp_perm", table_name="rbac_role_permissions")
    op.drop_index("ix_rbac_rp_role", table_name="rbac_role_permissions")
    op.drop_table("rbac_role_permissions")

    op.drop_index("ix_rbac_permissions_category", table_name="rbac_permissions")
    op.drop_index("ix_rbac_permissions_key", table_name="rbac_permissions")
    op.drop_table("rbac_permissions")

    op.drop_index("ix_rbac_roles_name", table_name="rbac_roles")
    op.drop_table("rbac_roles")
