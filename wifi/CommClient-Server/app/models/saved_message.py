"""
Saved (bookmarked) message model.

A user can bookmark any message they have access to. Bookmarks are private
to the user. Optionally a folder/label and a free-form note can be attached.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SavedMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "saved_messages"
    __table_args__ = (
        UniqueConstraint("user_id", "message_id", name="uq_saved_message"),
        Index("ix_saved_messages_user_folder", "user_id", "folder"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    folder: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship("User")  # noqa: F821
    message: Mapped["Message"] = relationship("Message")  # noqa: F821

    def __repr__(self) -> str:
        return f"<SavedMessage user={self.user_id[:8]} msg={self.message_id[:8]}>"
