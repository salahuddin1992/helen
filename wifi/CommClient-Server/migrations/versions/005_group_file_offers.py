"""Group file multicast offers

Revision ID: 005
Revises: 004
Create Date: 2026-04-18

Adds:
  * group_file_offers        — a single offer from a sender to every
    member of a channel. The file itself is uploaded once (file_id FK)
    and recipients either download from the server or participate in a
    peer swarm that fan-outs chunks P2P.
  * group_file_chunk_availability — a compact (offer_id, user_id) row
    tracking which chunks a peer currently holds. Used by the server to
    answer ``get_chunk_peers`` queries so any receiver can pull a chunk
    from the fastest / closest peer that already has it.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_file_offers",
        sa.Column("id", sa.String(32), primary_key=True, nullable=False),
        sa.Column(
            "sender_id",
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
            "file_id",
            sa.String(32),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("file_size", sa.BigInteger, nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("chunk_size", sa.BigInteger, nullable=False),
        sa.Column("total_chunks", sa.Integer, nullable=False),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("caption", sa.Text, nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'offered'"),
        ),  # offered | active | completed | cancelled | expired
        sa.Column("swarm_enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column(
            "accepted_count", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "rejected_count", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "completed_count", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "expected_recipients", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            index=True,
        ),
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

    # Composite index used by cleanup / admin dashboards.
    op.create_index(
        "idx_gfo_channel_status",
        "group_file_offers",
        ["channel_id", "status"],
    )
    op.create_index(
        "idx_gfo_status_expires",
        "group_file_offers",
        ["status", "expires_at"],
    )

    op.create_table(
        "group_file_chunk_availability",
        sa.Column(
            "offer_id",
            sa.String(32),
            sa.ForeignKey("group_file_offers.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        # Per-peer lifecycle. "declined" rows are kept so repeat offers
        # don't re-nag the user.
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),  # pending | accepted | completed | declined | abandoned
        # The chunk bitmap is packed 8 chunks per byte (LSB first) —
        # chunk_index i → bit (i % 8) of byte (i // 8). NULL == none yet.
        sa.Column("chunk_bitmap", sa.LargeBinary, nullable=True),
        sa.Column(
            "chunks_received", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "bytes_received", sa.BigInteger, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "last_progress_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
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
    op.create_index(
        "idx_gfca_offer_status",
        "group_file_chunk_availability",
        ["offer_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_gfca_offer_status", table_name="group_file_chunk_availability")
    op.drop_table("group_file_chunk_availability")
    op.drop_index("idx_gfo_status_expires", table_name="group_file_offers")
    op.drop_index("idx_gfo_channel_status", table_name="group_file_offers")
    op.drop_table("group_file_offers")
