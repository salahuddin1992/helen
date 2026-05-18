"""File acceptance — per-recipient file delivery/acceptance tracking

Revision ID: 003
Revises: 002
Create Date: 2026-04-18

Adds:
  * file_acceptances — one row per (file, recipient). Tracks the
    lifecycle of a shared file from pending → delivered → accepted /
    rejected, with byte-received progress and action timestamp.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_acceptances",
        sa.Column("id", sa.String(32), primary_key=True, nullable=False),
        sa.Column(
            "file_id",
            sa.String(32),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "message_id",
            sa.String(32),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "recipient_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "channel_id",
            sa.String(32),
            sa.ForeignKey("channels.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bytes_received", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "file_id", "recipient_id", name="uq_file_acceptance",
        ),
    )

    op.create_index(
        "ix_file_acceptance_channel_state",
        "file_acceptances",
        ["channel_id", "state"],
    )
    op.create_index(
        "ix_file_acceptance_recipient_state",
        "file_acceptances",
        ["recipient_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_file_acceptance_recipient_state", table_name="file_acceptances")
    op.drop_index("ix_file_acceptance_channel_state", table_name="file_acceptances")
    op.drop_table("file_acceptances")
