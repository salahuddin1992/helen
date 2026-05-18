"""Phase 5 / Module Y — Bridge tables.

Adds three tables:
    bridge_configs
    bridge_messages
    bridge_identities

Revision ID: helen_bridges_addon
Revises: helen_oauth_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_bridges_addon"
down_revision = "helen_oauth_addon"
branch_labels = ("helen_bridges_addon",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bridge_configs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("workspace_id", sa.String(length=32),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("channel_helen_id", sa.String(length=32), nullable=False),
        sa.Column("channel_remote_id", sa.String(length=128), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("last_status", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "workspace_id", "kind", "channel_helen_id", "channel_remote_id",
            name="uq_bridge_workspace_kind_channels",
        ),
    )
    op.create_index("ix_bridge_configs_workspace_id",
                    "bridge_configs", ["workspace_id"])
    op.create_index("ix_bridge_configs_kind", "bridge_configs", ["kind"])

    op.create_table(
        "bridge_messages",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("bridge_id", sa.String(length=32),
                  sa.ForeignKey("bridge_configs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("helen_message_id", sa.String(length=64), nullable=True),
        sa.Column("remote_message_id", sa.String(length=128), nullable=True),
        sa.Column("direction", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="queued"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_bridge_messages_bridge_id",
                    "bridge_messages", ["bridge_id"])
    op.create_index("ix_bridge_messages_helen_msg",
                    "bridge_messages", ["helen_message_id"])
    op.create_index("ix_bridge_messages_remote_msg",
                    "bridge_messages", ["remote_message_id"])
    op.create_index("ix_bridge_messages_created_at",
                    "bridge_messages", ["created_at"])

    op.create_table(
        "bridge_identities",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("bridge_id", sa.String(length=32),
                  sa.ForeignKey("bridge_configs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("helen_user_id", sa.String(length=32),
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("remote_user_id", sa.String(length=128), nullable=False),
        sa.Column("remote_username", sa.String(length=255), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("bridge_id", "remote_user_id",
                            name="uq_bridge_identity_remote"),
    )
    op.create_index("ix_bridge_identities_bridge_id",
                    "bridge_identities", ["bridge_id"])
    op.create_index("ix_bridge_identities_helen_user",
                    "bridge_identities", ["helen_user_id"])


def downgrade() -> None:
    op.drop_index("ix_bridge_identities_helen_user",
                  table_name="bridge_identities")
    op.drop_index("ix_bridge_identities_bridge_id",
                  table_name="bridge_identities")
    op.drop_table("bridge_identities")

    op.drop_index("ix_bridge_messages_created_at", table_name="bridge_messages")
    op.drop_index("ix_bridge_messages_remote_msg", table_name="bridge_messages")
    op.drop_index("ix_bridge_messages_helen_msg", table_name="bridge_messages")
    op.drop_index("ix_bridge_messages_bridge_id", table_name="bridge_messages")
    op.drop_table("bridge_messages")

    op.drop_index("ix_bridge_configs_kind", table_name="bridge_configs")
    op.drop_index("ix_bridge_configs_workspace_id", table_name="bridge_configs")
    op.drop_table("bridge_configs")
