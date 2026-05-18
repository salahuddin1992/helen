"""Phase 5 / Module Z — AI Assistant tables.

Adds four tables:
    ai_configs
    ai_sessions
    ai_messages
    ai_opt_ins

Revision ID: helen_ai_addon
Revises: helen_bridges_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_ai_addon"
down_revision = "helen_bridges_addon"
branch_labels = ("helen_ai_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_configs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False,
                  server_default="none"),
        sa.Column("model_name", sa.String(length=128), nullable=False,
                  server_default="claude-3-5-sonnet-latest"),
        sa.Column("api_key_secret_ref", sa.String(length=128), nullable=True),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("settings", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", name="uq_ai_config_workspace"),
    )
    op.create_index("ix_ai_configs_workspace_id",
                    "ai_configs", ["workspace_id"])

    op.create_table(
        "ai_sessions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("config_id", sa.String(length=32),
                  sa.ForeignKey("ai_configs.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ai_sessions_user_id", "ai_sessions", ["user_id"])
    op.create_index("ix_ai_sessions_workspace_id",
                    "ai_sessions", ["workspace_id"])
    op.create_index("ix_ai_sessions_kind", "ai_sessions", ["kind"])
    op.create_index("ix_ai_sessions_created_at",
                    "ai_sessions", ["created_at"])

    op.create_table(
        "ai_messages",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("session_id", sa.String(length=32),
                  sa.ForeignKey("ai_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("redacted_pii", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("cost_micro_usd", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ai_messages_session_id",
                    "ai_messages", ["session_id"])
    op.create_index("ix_ai_messages_created_at",
                    "ai_messages", ["created_at"])

    op.create_table(
        "ai_opt_ins",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False,
                  server_default="all"),
        sa.Column("opted_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_ai_optin"),
    )
    op.create_index("ix_ai_opt_ins_user_id", "ai_opt_ins", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_opt_ins_user_id", table_name="ai_opt_ins")
    op.drop_table("ai_opt_ins")

    op.drop_index("ix_ai_messages_created_at", table_name="ai_messages")
    op.drop_index("ix_ai_messages_session_id", table_name="ai_messages")
    op.drop_table("ai_messages")

    op.drop_index("ix_ai_sessions_created_at", table_name="ai_sessions")
    op.drop_index("ix_ai_sessions_kind", table_name="ai_sessions")
    op.drop_index("ix_ai_sessions_workspace_id", table_name="ai_sessions")
    op.drop_index("ix_ai_sessions_user_id", table_name="ai_sessions")
    op.drop_table("ai_sessions")

    op.drop_index("ix_ai_configs_workspace_id", table_name="ai_configs")
    op.drop_table("ai_configs")
