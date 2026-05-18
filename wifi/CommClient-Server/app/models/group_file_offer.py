"""
Group file multicast offer models.

These two tables implement a fan-out file-transfer primitive where a
single upload (``file_id``) is offered to an entire channel and every
member gets their own lifecycle row. Receivers either pull the file
straight from the server or swap chunks with peers (BitTorrent-style) to
relieve the sender's uplink.

Layout
------
``group_file_offers``
    One row per logical offer — the "outer envelope".

``group_file_chunk_availability``
    Composite primary key ``(offer_id, user_id)``. Tracks per-recipient
    state and a packed bitmap of which chunks the peer currently holds.

The bitmap uses the following packing convention (match the migration
comment exactly so the service and clients stay in sync):

    chunk_index i  →  bit (i % 8) of byte (i // 8), LSB first

NULL bitmap means "no chunks yet". A fully-complete peer has every bit
set up to ``total_chunks - 1`` — trailing bits in the last byte are
ignored.

State machines
--------------
``GroupFileOffer.status``
    ``offered`` → ``active`` → ``completed`` | ``cancelled`` | ``expired``

``GroupFileChunkAvailability.status``
    ``pending`` → ``accepted`` → ``completed``
    ``pending`` → ``declined``  (terminal)
    Anything non-terminal → ``abandoned``  (timeout / offer cancelled)

Both state vocabularies are ORM-friendly strings so they round-trip to
JSON without additional encoding.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


# ── Offer lifecycle ───────────────────────────────────────────
OFFER_STATUS_OFFERED = "offered"
OFFER_STATUS_ACTIVE = "active"
OFFER_STATUS_COMPLETED = "completed"
OFFER_STATUS_CANCELLED = "cancelled"
OFFER_STATUS_EXPIRED = "expired"

OFFER_ACTIVE_STATUSES = frozenset({OFFER_STATUS_OFFERED, OFFER_STATUS_ACTIVE})
OFFER_TERMINAL_STATUSES = frozenset({
    OFFER_STATUS_COMPLETED,
    OFFER_STATUS_CANCELLED,
    OFFER_STATUS_EXPIRED,
})
OFFER_VALID_STATUSES = OFFER_ACTIVE_STATUSES | OFFER_TERMINAL_STATUSES


# ── Per-recipient lifecycle ───────────────────────────────────
AVAIL_STATUS_PENDING = "pending"
AVAIL_STATUS_ACCEPTED = "accepted"
AVAIL_STATUS_COMPLETED = "completed"
AVAIL_STATUS_DECLINED = "declined"
AVAIL_STATUS_ABANDONED = "abandoned"

AVAIL_ACTIVE_STATUSES = frozenset({AVAIL_STATUS_PENDING, AVAIL_STATUS_ACCEPTED})
AVAIL_TERMINAL_STATUSES = frozenset({
    AVAIL_STATUS_COMPLETED,
    AVAIL_STATUS_DECLINED,
    AVAIL_STATUS_ABANDONED,
})
AVAIL_VALID_STATUSES = AVAIL_ACTIVE_STATUSES | AVAIL_TERMINAL_STATUSES


class GroupFileOffer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Envelope for a single fan-out file offer to a channel."""

    __tablename__ = "group_file_offers"

    sender_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Denormalized file metadata — kept on the offer so the UI can
    # render a preview without a second file-record lookup.
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OFFER_STATUS_OFFERED,
    )
    swarm_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Aggregate counters — denormalised for fast dashboard reads so we
    # don't have to scan ``group_file_chunk_availability`` per render.
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    # Audit fix H-2: per-chunk integrity. JSON list of base64-encoded
    # 8-byte SHA-256 prefixes (one per chunk index, ordered). Recipients
    # MAY verify each chunk's hash before marking it received. NULL means
    # the sender opted out (small files, dev mode, or legacy offers).
    # 8-byte prefix gives ~64-bit collision resistance — enough for
    # transport-tampering detection without bloating the offer payload.
    chunk_hashes_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )

    __table_args__ = (
        Index("idx_gfo_channel_status", "channel_id", "status"),
        Index("idx_gfo_status_expires", "status", "expires_at"),
    )

    # ── Relationships ──────────────────────────────
    sender = relationship("User", foreign_keys=[sender_id])
    channel = relationship("Channel", foreign_keys=[channel_id])
    file_record = relationship("FileRecord", foreign_keys=[file_id])
    availabilities: Mapped[list["GroupFileChunkAvailability"]] = relationship(
        "GroupFileChunkAvailability",
        back_populates="offer",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ── Domain helpers ─────────────────────────────

    def is_active(self) -> bool:
        return self.status in OFFER_ACTIVE_STATUSES

    def is_terminal(self) -> bool:
        return self.status in OFFER_TERMINAL_STATUSES

    def mark_status(self, new_status: str) -> bool:
        """Transition to ``new_status`` if valid and not terminal already."""
        if new_status not in OFFER_VALID_STATUSES:
            raise ValueError(f"invalid offer status: {new_status!r}")
        if self.status == new_status:
            return False
        if self.is_terminal():
            # Offers don't resurrect.
            return False
        self.status = new_status
        return True

    def to_dict(self, *, include_counts: bool = True) -> dict:
        base = {
            "id": self.id,
            "sender_id": self.sender_id,
            "channel_id": self.channel_id,
            "file_id": self.file_id,
            "filename": self.filename,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "chunk_size": self.chunk_size,
            "total_chunks": self.total_chunks,
            "checksum": self.checksum,
            "caption": self.caption,
            "status": self.status,
            "swarm_enabled": bool(self.swarm_enabled),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_counts:
            base.update({
                "accepted_count": self.accepted_count,
                "rejected_count": self.rejected_count,
                "completed_count": self.completed_count,
                "expected_recipients": self.expected_recipients,
            })
        return base

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<GroupFileOffer id={self.id[:8]} ch={self.channel_id[:8]} "
            f"file={self.file_id[:8]} status={self.status}>"
        )


class GroupFileChunkAvailability(Base, TimestampMixin):
    """
    Per-recipient slot for a ``GroupFileOffer``.

    Holds the chunk bitmap + progress counters + transfer lifecycle so
    the server can answer ``who-has-chunk-N`` swarm queries and track
    completion without scanning the blob store.
    """

    __tablename__ = "group_file_chunk_availability"

    offer_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("group_file_offers.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AVAIL_STATUS_PENDING,
    )
    # Packed bitmap — 8 chunks per byte, LSB first. ``None`` means the
    # peer hasn't reported any chunks yet. Kept ``NULLable`` rather than
    # an empty bytes() so we can distinguish "never reported" (no swarm
    # participation possible) from "explicitly zero bytes available".
    chunk_bitmap: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    chunks_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    last_progress_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        Index("idx_gfca_offer_status", "offer_id", "status"),
    )

    # ── Relationships ──────────────────────────────
    offer: Mapped["GroupFileOffer"] = relationship(
        "GroupFileOffer", back_populates="availabilities", foreign_keys=[offer_id],
    )
    user = relationship("User", foreign_keys=[user_id])

    # ── Bitmap helpers ─────────────────────────────
    # These MUST stay in lockstep with the packing convention documented
    # at the top of this module — clients rely on the same layout.

    @staticmethod
    def _required_bytes(total_chunks: int) -> int:
        if total_chunks <= 0:
            return 0
        return (total_chunks + 7) // 8

    def ensure_bitmap(self, total_chunks: int) -> bytearray:
        """Return a mutable bitmap big enough to address ``total_chunks``."""
        needed = self._required_bytes(total_chunks)
        cur = bytearray(self.chunk_bitmap) if self.chunk_bitmap else bytearray()
        if len(cur) < needed:
            cur.extend(b"\x00" * (needed - len(cur)))
        return cur

    def has_chunk(self, chunk_index: int) -> bool:
        if chunk_index < 0 or not self.chunk_bitmap:
            return False
        byte_idx, bit_idx = divmod(chunk_index, 8)
        if byte_idx >= len(self.chunk_bitmap):
            return False
        return bool(self.chunk_bitmap[byte_idx] & (1 << bit_idx))

    def set_chunk(self, chunk_index: int, total_chunks: int) -> bool:
        """
        Set bit for ``chunk_index``. Returns True if the bit flipped from
        0 → 1 (so the caller can decide whether to increment counters).
        """
        if chunk_index < 0 or chunk_index >= total_chunks:
            raise ValueError(
                f"chunk_index {chunk_index} out of range [0, {total_chunks})"
            )
        bm = self.ensure_bitmap(total_chunks)
        byte_idx, bit_idx = divmod(chunk_index, 8)
        mask = 1 << bit_idx
        already = bool(bm[byte_idx] & mask)
        if already:
            # Still persist — no-op update keeps the column non-NULL so
            # "I have exactly the same bitmap as before" is still visible
            # to swarm queries.
            self.chunk_bitmap = bytes(bm)
            return False
        bm[byte_idx] |= mask
        self.chunk_bitmap = bytes(bm)
        return True

    def held_chunk_indexes(self, total_chunks: int) -> list[int]:
        """Decoded list of chunk indexes this peer currently holds."""
        if not self.chunk_bitmap:
            return []
        out: list[int] = []
        bm = self.chunk_bitmap
        limit = min(total_chunks, len(bm) * 8)
        for i in range(limit):
            byte_idx, bit_idx = divmod(i, 8)
            if bm[byte_idx] & (1 << bit_idx):
                out.append(i)
        return out

    def is_complete(self, total_chunks: int) -> bool:
        if total_chunks <= 0:
            return True
        received = self.chunks_received or 0
        if received >= total_chunks:
            return True
        if not self.chunk_bitmap:
            return False
        bm = self.chunk_bitmap
        for i in range(total_chunks):
            byte_idx, bit_idx = divmod(i, 8)
            if byte_idx >= len(bm):
                return False
            if not (bm[byte_idx] & (1 << bit_idx)):
                return False
        return True

    # ── Lifecycle helpers ──────────────────────────

    def is_active(self) -> bool:
        return self.status in AVAIL_ACTIVE_STATUSES

    def is_terminal(self) -> bool:
        return self.status in AVAIL_TERMINAL_STATUSES

    def mark_status(self, new_status: str) -> bool:
        if new_status not in AVAIL_VALID_STATUSES:
            raise ValueError(f"invalid availability status: {new_status!r}")
        if self.status == new_status:
            return False
        if self.is_terminal():
            return False
        self.status = new_status
        return True

    def to_dict(self, *, total_chunks: int | None = None) -> dict:
        data = {
            "offer_id": self.offer_id,
            "user_id": self.user_id,
            "status": self.status,
            "chunks_received": self.chunks_received,
            "bytes_received": self.bytes_received,
            "last_progress_at": (
                self.last_progress_at.isoformat() if self.last_progress_at else None
            ),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if total_chunks is not None:
            data["held_chunks"] = self.held_chunk_indexes(total_chunks)
            data["is_complete"] = self.is_complete(total_chunks)
        return data

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<GroupFileChunkAvailability offer={self.offer_id[:8]} "
            f"user={self.user_id[:8]} status={self.status} "
            f"chunks={self.chunks_received}>"
        )


__all__ = [
    "GroupFileOffer",
    "GroupFileChunkAvailability",
    "OFFER_STATUS_OFFERED",
    "OFFER_STATUS_ACTIVE",
    "OFFER_STATUS_COMPLETED",
    "OFFER_STATUS_CANCELLED",
    "OFFER_STATUS_EXPIRED",
    "OFFER_ACTIVE_STATUSES",
    "OFFER_TERMINAL_STATUSES",
    "OFFER_VALID_STATUSES",
    "AVAIL_STATUS_PENDING",
    "AVAIL_STATUS_ACCEPTED",
    "AVAIL_STATUS_COMPLETED",
    "AVAIL_STATUS_DECLINED",
    "AVAIL_STATUS_ABANDONED",
    "AVAIL_ACTIVE_STATUSES",
    "AVAIL_TERMINAL_STATUSES",
    "AVAIL_VALID_STATUSES",
]
