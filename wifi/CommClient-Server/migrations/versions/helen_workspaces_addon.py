"""Phase 3 / Module M — Multi-tenant workspaces.

Adds three tables:
    workspaces
    workspace_members
    workspace_invites

Revision ID: helen_workspaces_addon
Revises: helen_agents_addon
Create Date: 2026-05-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_workspaces_addon"
down_revision = "helen_agents_addon"
branch_labels = ("helen_workspaces_addon",)
depends_on = None


def upgrade() -> None:
    # ── workspaces ───────────────────────────────────────────
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan", sa.String(length=32), nullable=False, server_default="free"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_workspaces_slug", "workspaces", ["slug"], unique=True)
    op.create_index("ix_workspaces_owner_id", "workspaces", ["owner_id"])

    # ── workspace_members ────────────────────────────────────
    op.create_table(
        "workspace_members",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False,
                  server_default="member"),
        sa.Column("joined_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("invited_by", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
    )
    op.create_index("ix_workspace_members_workspace_id",
                    "workspace_members", ["workspace_id"])
    op.create_index("ix_workspace_members_user_id",
                    "workspace_members", ["user_id"])

    # ── workspace_invites ────────────────────────────────────
    op.create_table(
        "workspace_invites",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False,
                  server_default="member"),
        sa.Column("issued_by", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_workspace_invites_workspace_id",
                    "workspace_invites", ["workspace_id"])
    op.create_index("ix_workspace_invites_code",
                    "workspace_invites", ["code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_workspace_invites_code", table_name="workspace_invites")
    op.drop_index("ix_workspace_invites_workspace_id", table_name="workspace_invites")
    op.drop_table("workspace_invites")

    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_index("ix_workspace_members_workspace_id", table_name="workspace_members")
    op.drop_table("workspace_members")

    op.drop_index("ix_workspaces_owner_id", table_name="workspaces")
    op.drop_index("ix_workspaces_slug", table_name="workspaces")
    op.drop_table("workspaces")
