"""Active call persistence + resumable uploads + hybrid topology

Revision ID: 002
Revises: 001
Create Date: 2026-04-17

Adds:
  * active_calls            — live call rows (survive restart)
  * active_call_participants — live membership per call
  * call_signal_events      — signaling replay log
  * upload_sessions         — resumable upload sessions
  * upload_chunks           — per-chunk integrity (CRC32 + SHA-256)

Safe to run on an existing DB — every op is create-only and uses
`IF NOT EXISTS` via ``op.create_table`` default behavior on SQLite.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── active_calls ─────────────────────────────────────────────
    op.create_table(
        "active_calls",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("channel_id", sa.String(32), sa.ForeignKey("channels.id", ondelete="SET NULL"), nullable=True),
        sa.Column("initiator_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("call_type", sa.String(16), nullable=False),
        sa.Column("routing", sa.String(8), nullable=False, server_default="mesh"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ringing"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("max_participants", sa.Integer, nullable=False, server_default="1"),
        sa.Column("topology_generation", sa.Integer, nullable=False, server_default="1"),
        sa.Column("metadata_json", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_active_calls_channel_id", "active_calls", ["channel_id"])
    op.create_index("ix_active_calls_initiator_id", "active_calls", ["initiator_id"])
    op.create_index("ix_active_calls_status", "active_calls", ["status"])
    op.create_index("ix_active_calls_last_heartbeat_at", "active_calls", ["last_heartbeat_at"])
    op.create_index("ix_active_calls_status_heartbeat", "active_calls", ["status", "last_heartbeat_at"])
    op.create_index("ix_active_calls_channel_status", "active_calls", ["channel_id", "status"])

    # ── active_call_participants ────────────────────────────────
    op.create_table(
        "active_call_participants",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("call_id", sa.String(32), sa.ForeignKey("active_calls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sid", sa.String(128), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="participant"),
        sa.Column("muted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("video_off", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("sharing_screen", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("on_hold", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_quality_json", sa.Text, nullable=True),
        sa.Column("last_quality_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_id", "user_id", name="uq_active_call_participant"),
    )
    op.create_index("ix_active_call_participants_call_id", "active_call_participants", ["call_id"])
    op.create_index("ix_active_call_participants_user_id", "active_call_participants", ["user_id"])
    op.create_index("ix_active_call_participants_sid", "active_call_participants", ["sid"])
    op.create_index("ix_active_participant_user_live", "active_call_participants", ["user_id", "left_at"])

    # ── call_signal_events ──────────────────────────────────────
    op.create_table(
        "call_signal_events",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("call_id", sa.String(32), sa.ForeignKey("active_calls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_user", sa.String(32), nullable=False),
        sa.Column("to_user", sa.String(32), nullable=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("topology_generation", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_call_signal_events_call_id", "call_signal_events", ["call_id"])
    op.create_index("ix_call_signal_events_from_user", "call_signal_events", ["from_user"])
    op.create_index("ix_call_signal_events_to_user", "call_signal_events", ["to_user"])
    op.create_index("ix_call_signal_call_kind", "call_signal_events", ["call_id", "kind"])

    # ── upload_sessions ─────────────────────────────────────────
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.String(32), sa.ForeignKey("channels.id", ondelete="SET NULL"), nullable=True),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("total_size", sa.BigInteger, nullable=False),
        sa.Column("chunk_size", sa.Integer, nullable=False, server_default="262144"),
        sa.Column("total_chunks", sa.Integer, nullable=False),
        sa.Column("expected_sha256", sa.String(64), nullable=True),
        sa.Column("computed_sha256", sa.String(64), nullable=True),
        sa.Column("received_chunks", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bytes_received", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="init"),
        sa.Column("file_record_id", sa.String(32), sa.ForeignKey("files.id", ondelete="SET NULL"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(255), nullable=True),
        sa.Column("staging_path", sa.String(512), nullable=False),
        sa.Column("metadata_json", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_upload_sessions_owner_id", "upload_sessions", ["owner_id"])
    op.create_index("ix_upload_sessions_channel_id", "upload_sessions", ["channel_id"])
    op.create_index("ix_upload_sessions_status", "upload_sessions", ["status"])
    op.create_index("ix_upload_sessions_expires_at", "upload_sessions", ["expires_at"])
    op.create_index("ix_upload_sessions_owner_status", "upload_sessions", ["owner_id", "status"])
    op.create_index("ix_upload_sessions_expires_status", "upload_sessions", ["expires_at", "status"])

    # ── upload_chunks ───────────────────────────────────────────
    op.create_table(
        "upload_chunks",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("session_id", sa.String(32), sa.ForeignKey("upload_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("offset", sa.BigInteger, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("crc32", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "chunk_index", name="uq_upload_chunk_index"),
    )
    op.create_index("ix_upload_chunks_session_id", "upload_chunks", ["session_id"])
    op.create_index("ix_upload_chunk_session_idx", "upload_chunks", ["session_id", "chunk_index"])


def downgrade() -> None:
    op.drop_table("upload_chunks")
    op.drop_table("upload_sessions")
    op.drop_table("call_signal_events")
    op.drop_table("active_call_participants")
    op.drop_table("active_calls")
