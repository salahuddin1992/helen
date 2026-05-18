"""Module L — Helen Agent addon.

Adds three tables for the device-agent subsystem:
    agents
    agent_events
    agent_commands

Runs after ``helen_rbac_addon``. Nothing is removed from the existing schema.

Revision ID: helen_agents_addon
Revises: helen_rbac_addon
Create Date: 2026-05-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_agents_addon"
down_revision = "helen_rbac_addon"
branch_labels = ("helen_agents_addon",)
depends_on = None


def upgrade() -> None:
    # ── agents ───────────────────────────────────────────────
    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("hostname", sa.String(length=256), nullable=False,
                  server_default=sa.text("'unknown'")),
        sa.Column("os_name", sa.String(length=64), nullable=True),
        sa.Column("os_version", sa.String(length=128), nullable=True),
        sa.Column("agent_version", sa.String(length=32), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'offline'")),
        sa.Column("workspace_id", sa.String(length=32), nullable=True),
        sa.Column("refresh_token_hash", sa.String(length=128), nullable=True),
        sa.Column("refresh_token_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token_version", sa.Integer(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("public_key", sa.Text(), nullable=True),
        sa.Column("last_snapshot_json", sa.Text(), nullable=True),
        sa.Column("last_ip", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("fingerprint", name="uq_agents_fingerprint"),
    )
    op.create_index("ix_agents_fingerprint", "agents", ["fingerprint"])
    op.create_index("ix_agents_status", "agents", ["status"])
    op.create_index("ix_agents_workspace_id", "agents", ["workspace_id"])
    op.create_index("ix_agents_last_heartbeat_at", "agents", ["last_heartbeat_at"])
    op.create_index(
        "ix_agents_status_last_heartbeat",
        "agents",
        ["status", "last_heartbeat_at"],
    )

    # ── agent_events ─────────────────────────────────────────
    op.create_table(
        "agent_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("agent_id", sa.String(length=32),
                  sa.ForeignKey("agents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_agent_events_agent_id", "agent_events", ["agent_id"])
    op.create_index(
        "ix_agent_events_agent_created",
        "agent_events",
        ["agent_id", "created_at"],
    )
    op.create_index("ix_agent_events_type", "agent_events", ["event_type"])

    # ── agent_commands ───────────────────────────────────────
    op.create_table(
        "agent_commands",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("agent_id", sa.String(length=32),
                  sa.ForeignKey("agents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("command", sa.String(length=128), nullable=False),
        sa.Column("args_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'queued'")),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("issued_by", sa.String(length=32), nullable=False,
                  server_default=sa.text("'system'")),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeout_secs", sa.Integer(), nullable=False,
                  server_default=sa.text("30")),
    )
    op.create_index("ix_agent_commands_agent_id", "agent_commands", ["agent_id"])
    op.create_index("ix_agent_commands_status", "agent_commands", ["status"])
    op.create_index(
        "ix_agent_commands_agent_status",
        "agent_commands",
        ["agent_id", "status"],
    )
    op.create_index("ix_agent_commands_issued_at", "agent_commands", ["issued_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_commands_issued_at", table_name="agent_commands")
    op.drop_index("ix_agent_commands_agent_status", table_name="agent_commands")
    op.drop_index("ix_agent_commands_status", table_name="agent_commands")
    op.drop_index("ix_agent_commands_agent_id", table_name="agent_commands")
    op.drop_table("agent_commands")

    op.drop_index("ix_agent_events_type", table_name="agent_events")
    op.drop_index("ix_agent_events_agent_created", table_name="agent_events")
    op.drop_index("ix_agent_events_agent_id", table_name="agent_events")
    op.drop_table("agent_events")

    op.drop_index("ix_agents_status_last_heartbeat", table_name="agents")
    op.drop_index("ix_agents_last_heartbeat_at", table_name="agents")
    op.drop_index("ix_agents_workspace_id", table_name="agents")
    op.drop_index("ix_agents_status", table_name="agents")
    op.drop_index("ix_agents_fingerprint", table_name="agents")
    op.drop_table("agents")
