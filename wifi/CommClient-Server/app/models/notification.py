"""
Notification model — delivery of user-relevant events (messages, calls, invites).
Supports multiple notification types with optional references to related entities.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class Notification(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("idx_user_id_created_at", "user_id", "created_at"),
        Index("idx_user_id_is_read", "user_id", "is_read"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )  # "message", "call_missed", "call_incoming", "contact_request", "group_invite", "system", "mention"
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_id: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )  # ID of related entity (message_id, call_id, channel_id, etc.)
    reference_type: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )  # "message", "call", "channel", "user", "contact"
    is_read: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="notifications")

    def __repr__(self) -> str:
        return f"<Notification {self.id[:8]} type={self.type} user={self.user_id[:8]}>"
