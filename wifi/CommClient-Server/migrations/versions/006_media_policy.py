"""Media policy, per-user overrides, and ingest sources

Revision ID: 006
Revises: 005
Create Date: 2026-04-19

Adds:
  * media_policies         — singleton ('global') row holding global caps
                             on resolution / framerate / bitrate plus a JSON
                             blob of per-role caps.
  * user_media_overrides   — per-user overrides that win over role caps.
  * ingest_sources         — external camera feeds (RTSP/RTMP/SRT/HTTP/NDI)
                             supervised by the FFmpeg ingest service.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_policies",
        sa.Column("id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("global_max_width", sa.Integer, nullable=False, server_default=sa.text("1920")),
        sa.Column("global_max_height", sa.Integer, nullable=False, server_default=sa.text("1080")),
        sa.Column("global_max_framerate", sa.Integer, nullable=False, server_default=sa.text("30")),
        sa.Column("global_max_bitrate_kbps", sa.Integer, nullable=False, server_default=sa.text("10000")),
        sa.Column("allow_8k", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("allow_client_override", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("enforce_hard_cap", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("role_caps_json", sa.Text, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("transcoding_enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("prefer_hw_encoder", sa.Boolean, nullable=False, server_default=sa.text("1")),
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

    op.create_table(
        "user_media_overrides",
        sa.Column("id", sa.String(32), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column("max_width", sa.Integer, nullable=True),
        sa.Column("max_height", sa.Integer, nullable=True),
        sa.Column("max_framerate", sa.Integer, nullable=True),
        sa.Column("max_bitrate_kbps", sa.Integer, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
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

    op.create_table(
        "ingest_sources",
        sa.Column("id", sa.String(32), primary_key=True, nullable=False),
        sa.Column(
            "owner_user_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("username", sa.String(128), nullable=True),
        sa.Column("password", sa.String(256), nullable=True),
        sa.Column("transport", sa.String(8), nullable=False, server_default=sa.text("'tcp'")),
        sa.Column("codec_hint", sa.String(16), nullable=True),
        sa.Column("target_width", sa.Integer, nullable=True),
        sa.Column("target_height", sa.Integer, nullable=True),
        sa.Column("target_framerate", sa.Integer, nullable=True),
        sa.Column("target_bitrate_kbps", sa.Integer, nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("auto_start", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'idle'")),
        sa.Column("last_error", sa.Text, nullable=True),
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

    # Seed the singleton global policy so callers never see a NULL row.
    # Use exec_driver_sql so SQLAlchemy doesn't try to parse the JSON colons
    # (":") as named bind-parameter markers.
    op.get_bind().exec_driver_sql(
        """
        INSERT INTO media_policies (
            id, global_max_width, global_max_height, global_max_framerate,
            global_max_bitrate_kbps, allow_8k, allow_client_override,
            enforce_hard_cap, role_caps_json, transcoding_enabled,
            prefer_hw_encoder
        ) VALUES (
            'global', 7680, 4320, 60,
            80000, 1, 1,
            1,
            '{"admin":{"max_w":7680,"max_h":4320,"max_fps":60,"max_kbps":80000},"moderator":{"max_w":3840,"max_h":2160,"max_fps":60,"max_kbps":40000},"user":{"max_w":7680,"max_h":4320,"max_fps":60,"max_kbps":80000}}',
            1,
            1
        )
        """
    )


def downgrade() -> None:
    op.drop_table("ingest_sources")
    op.drop_table("user_media_overrides")
    op.drop_table("media_policies")
