"""Messaging dead-letter queue

Revision ID: 004
Revises: 003
Create Date: 2026-04-18

Adds:
  * message_dead_letters — persistent record of messaging side-effect
    failures (fan-out, webhook, push, scheduled) with replay metadata.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_dead_letters",
        sa.Column("id", sa.String(32), primary_key=True, nullable=False),
        sa.Column(
            "message_id",
            sa.String(32),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "channel_id",
            sa.String(32),
            sa.ForeignKey("channels.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "sender_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("operator_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # Composite indexes for common admin queries
    op.create_index(
        "idx_dlq_status_next_attempt",
        "message_dead_letters",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "idx_dlq_kind_status",
        "message_dead_letters",
        ["kind", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_dlq_kind_status", table_name="message_dead_letters")
    op.drop_index("idx_dlq_status_next_attempt", table_name="message_dead_letters")
    op.drop_table("message_dead_letters")
