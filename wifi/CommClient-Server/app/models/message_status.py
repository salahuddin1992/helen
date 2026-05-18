"""
MessageReceipt model — per-recipient delivery and read tracking.

Tracks when each message was delivered to and read by each recipient.
Separates receipt tracking from the Message model for scalability.

Schema:
  message_receipts (
    id             UUID PK,
    message_id     UUID FK → messages.id,
    recipient_id   UUID FK → users.id,
    delivered_at   DateTime nullable,
    read_at        DateTime nullable,
    created_at     DateTime,
  )

  UNIQUE (message_id, recipient_id) — one receipt per message per recipient.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship

from app.db.base import Base, UUIDPrimaryKeyMixin, TimestampMixin


class MessageReceipt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "message_receipts"

    message_id = Column(
        String(32),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recipient_id = Column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("message_id", "recipient_id", name="uq_message_receipt"),
        Index("ix_receipt_recipient_delivered", "recipient_id", "delivered_at"),
        Index("ix_receipt_message_read", "message_id", "read_at"),
    )

    # Relationships
    message = relationship("Message", backref="receipts")
    recipient = relationship("User")

    def mark_delivered(self) -> None:
        if not self.delivered_at:
            self.delivered_at = datetime.now(timezone.utc)

    def mark_read(self) -> None:
        now = datetime.now(timezone.utc)
        if not self.delivered_at:
            self.delivered_at = now
        if not self.read_at:
            self.read_at = now

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "recipient_id": self.recipient_id,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
        }
