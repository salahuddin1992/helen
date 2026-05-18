"""
Message edit history — append-only log of every edit performed on a message.
Each row records the prior content (the value the message held BEFORE the edit
was applied), the editor, and when the edit happened.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now


class MessageEditHistory(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "message_edit_history"
    __table_args__ = (
        Index("ix_message_edit_history_msg_time", "message_id", "edited_at"),
    )

    message_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    editor_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Snapshot of message content BEFORE the edit
    previous_content: Mapped[str] = mapped_column(Text, nullable=False)
    edited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False,
    )

    message: Mapped["Message"] = relationship("Message")  # noqa: F821
    editor: Mapped["User | None"] = relationship("User")  # noqa: F821

    def __repr__(self) -> str:
        return f"<MessageEditHistory msg={self.message_id[:8]} at={self.edited_at}>"
