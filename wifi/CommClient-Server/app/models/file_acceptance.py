"""
FileAcceptance model — per-recipient state for shared files in a channel.

Group file transfers need more than a single completion record. Each
recipient has their own lifecycle over the same FileRecord:

  * pending    — file is available, recipient hasn't acted on it.
  * delivered  — client reported "downloaded/streamed the bytes".
  * accepted   — recipient explicitly accepted / saved the file.
  * rejected   — recipient explicitly declined the file.

Separating this from MessageReceipt keeps file-acceptance flows
independent of read receipts for the carrying chat message — a user can
read the notification message without saving the file, or vice versa.

Schema:
  file_acceptances (
    id             UUID PK,
    file_id        UUID FK → files.id,
    message_id     UUID FK → messages.id (nullable — file can exist without message),
    recipient_id   UUID FK → users.id,
    channel_id     UUID FK → channels.id (indexed, for per-channel queries),
    state          VARCHAR(16) — pending|delivered|accepted|rejected
    delivered_at   DateTime nullable,
    acted_at       DateTime nullable,       # when accepted / rejected
    bytes_received BIGINT default 0,        # for progress tracking
    created_at     DateTime,
  )

  UNIQUE (file_id, recipient_id) — one row per file per recipient.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db.base import Base, UUIDPrimaryKeyMixin, TimestampMixin


# State constants — keep as strings (ORM-friendly + easy to serialize).
STATE_PENDING = "pending"
STATE_DELIVERED = "delivered"
STATE_ACCEPTED = "accepted"
STATE_REJECTED = "rejected"

VALID_STATES = frozenset({
    STATE_PENDING,
    STATE_DELIVERED,
    STATE_ACCEPTED,
    STATE_REJECTED,
})

# Terminal states — no further transitions allowed.
TERMINAL_STATES = frozenset({STATE_ACCEPTED, STATE_REJECTED})


class FileAcceptance(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "file_acceptances"

    file_id = Column(
        String(32),
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id = Column(
        String(32),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    recipient_id = Column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id = Column(
        String(32),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state = Column(
        String(16),
        nullable=False,
        default=STATE_PENDING,
    )
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    acted_at = Column(DateTime(timezone=True), nullable=True)
    bytes_received = Column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("file_id", "recipient_id", name="uq_file_acceptance"),
        Index("ix_file_acceptance_channel_state", "channel_id", "state"),
        Index("ix_file_acceptance_recipient_state", "recipient_id", "state"),
    )

    # Relationships
    file_record = relationship("FileRecord", foreign_keys=[file_id])
    message = relationship("Message", foreign_keys=[message_id])
    recipient = relationship("User", foreign_keys=[recipient_id])
    channel = relationship("Channel", foreign_keys=[channel_id])

    # ── Domain helpers ───────────────────────────────────────────

    def mark_delivered(self, *, bytes_received: int | None = None) -> bool:
        """
        Mark the file as delivered to this recipient.

        Returns True when the state actually advanced — callers can use
        that to decide whether to fan out a ``file_delivered`` event.
        """
        if self.state in TERMINAL_STATES:
            # Already accepted / rejected — but still update bytes_received.
            if bytes_received is not None:
                self.bytes_received = max(self.bytes_received, bytes_received)
            return False

        advanced = False
        if self.state == STATE_PENDING:
            self.state = STATE_DELIVERED
            advanced = True

        if not self.delivered_at:
            self.delivered_at = datetime.now(timezone.utc)

        if bytes_received is not None:
            self.bytes_received = max(self.bytes_received, bytes_received)

        return advanced

    def mark_accepted(self) -> bool:
        if self.state == STATE_ACCEPTED:
            return False
        if self.state == STATE_REJECTED:
            # Explicitly refuse to bounce back from a rejection — force a
            # new acceptance row via the service if the recipient changes
            # their mind after rejecting.
            return False
        now = datetime.now(timezone.utc)
        if not self.delivered_at:
            self.delivered_at = now
        self.acted_at = now
        self.state = STATE_ACCEPTED
        return True

    def mark_rejected(self) -> bool:
        if self.state in TERMINAL_STATES:
            return False
        self.acted_at = datetime.now(timezone.utc)
        self.state = STATE_REJECTED
        return True

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "message_id": self.message_id,
            "recipient_id": self.recipient_id,
            "channel_id": self.channel_id,
            "state": self.state,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "acted_at": self.acted_at.isoformat() if self.acted_at else None,
            "bytes_received": self.bytes_received,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
