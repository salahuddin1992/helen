"""profile_photos table

Revision ID: 008
Revises: 007
Create Date: 2026-04-20

Adds multi-photo profile gallery with per-photo visibility (public / contacts /
private) and an is_primary flag that controls which photo mirrors into
users.avatar_url for legacy consumers.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profile_photos",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("storage_name", sa.String(128), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "visibility",
            sa.String(16),
            nullable=False,
            server_default="public",
        ),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("caption", sa.Text(), nullable=True),
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
    )
    op.create_index(
        "ix_profile_photos_user_id",
        "profile_photos",
        ["user_id"],
    )
    op.create_index(
        "ix_profile_photos_user_position",
        "profile_photos",
        ["user_id", "position"],
    )


def downgrade() -> None:
    op.drop_index("ix_profile_photos_user_position", table_name="profile_photos")
    op.drop_index("ix_profile_photos_user_id", table_name="profile_photos")
    op.drop_table("profile_photos")
