"""
Message model — text, file, system events, replies, reactions.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Message(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "messages"

    channel_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    sender_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="text",
    )  # "text", "file", "image", "system", "reply"
    reply_to: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True,
    )
    file_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("files.id", ondelete="SET NULL"), nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="sent",
    )  # "sent", "delivered", "read"
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    pinned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    pinned_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    forwarded_from: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True,
    )
    # Client-supplied idempotency key. Combined with sender_id, this
    # is the dedup boundary for retries: any client that disconnects
    # mid-send and re-emits the same client_message_id gets the
    # original message back instead of a duplicate row. Indexed via
    # the table-level UniqueConstraint below. NULL is allowed for
    # legacy / system-emitted messages that don't go through a client.
    client_message_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )

    __table_args__ = (
        # Two messages with the same (sender_id, client_message_id) are
        # treated as the same message — the second INSERT collapses to
        # an UPDATE-noop in MessageService.send_message.
        UniqueConstraint(
            "sender_id", "client_message_id",
            name="uq_message_sender_client_id",
        ),
    )

    # Relationships
    channel: Mapped["Channel"] = relationship("Channel", back_populates="messages")
    sender: Mapped["User"] = relationship("User", back_populates="sent_messages", foreign_keys=[sender_id])
    reactions: Mapped[list["Reaction"]] = relationship(
        "Reaction", back_populates="message", cascade="all, delete-orphan",
    )
    replied_message: Mapped["Message | None"] = relationship(
        "Message", remote_side="Message.id", foreign_keys=[reply_to],
    )
    file: Mapped["FileRecord | None"] = relationship("FileRecord", foreign_keys=[file_id])
    pinned_by_user: Mapped["User | None"] = relationship("User", foreign_keys=[pinned_by])
    forwarded_original: Mapped["Message | None"] = relationship(
        "Message", remote_side="Message.id", foreign_keys=[forwarded_from],
    )

    def __repr__(self) -> str:
        return f"<Message {self.id[:8]} in {self.channel_id[:8]}>"


class Reaction(Base, TimestampMixin):
    __tablename__ = "reactions"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", "emoji", name="uq_reaction"),
    )

    message_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    emoji: Mapped[str] = mapped_column(String(32), primary_key=True, nullable=False)

    # Relationships
    message: Mapped["Message"] = relationship("Message", back_populates="reactions")
    user: Mapped["User"] = relationship("User")

    def __repr__(self) -> str:
        return f"<Reaction {self.emoji} on {self.message_id[:8]}>"
