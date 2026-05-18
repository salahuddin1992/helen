"""
Scheduled message model — messages queued for future delivery.

A background worker periodically scans for entries whose `send_at` has passed
and dispatches them through MessageService.send_message, then marks them as
sent. Failed deliveries are retried with backoff and eventually marked failed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class ScheduledMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "scheduled_messages"

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
    content: Mapped[str] = mapped_column(Text, nullable=False)
    msg_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="text",
    )
    reply_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    file_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Delivery target time (UTC)
    send_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )

    # State machine: pending → sent | failed | cancelled
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True,
    )

    # Set when status transitions to "sent"
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Set when status transitions to "sent" — points to the real Message row
    delivered_message_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )

    # Retry tracking
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        Index("ix_scheduled_messages_pending", "status", "send_at"),
        Index("ix_scheduled_messages_sender_status", "sender_id", "status"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender_id": self.sender_id,
            "channel_id": self.channel_id,
            "content": self.content,
            "msg_type": self.msg_type,
            "reply_to": self.reply_to,
            "file_id": self.file_id,
            "send_at": self.send_at.isoformat() if self.send_at else None,
            "status": self.status,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "delivered_message_id": self.delivered_message_id,
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
