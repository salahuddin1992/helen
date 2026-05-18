"""
Message draft model — per-user, per-channel (and optionally per-thread)
unsent message storage. Drafts are private to the user, restored on reconnect
by the client.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MessageDraft(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "message_drafts"
    __table_args__ = (
        # One draft per (user, channel, thread_root). NULL thread_root collates
        # to a single channel-level draft.
        UniqueConstraint(
            "user_id", "channel_id", "thread_root_id", name="uq_message_draft_scope"
        ),
        Index("ix_message_drafts_user", "user_id"),
        Index("ix_message_drafts_user_channel", "user_id", "channel_id"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    thread_root_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Free-form JSON payload (mentions, attachments, formatting state)
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship("User")  # noqa: F821
    channel: Mapped["Channel"] = relationship("Channel")  # noqa: F821

    def __repr__(self) -> str:
        return f"<MessageDraft user={self.user_id[:8]} ch={self.channel_id[:8]}>"
