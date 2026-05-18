"""
Channel model — DM (1-to-1) and Group channels.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class Channel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "channels"

    type: Mapped[str] = mapped_column(
        String(16), nullable=False,
    )  # "dm" or "group"
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Relationships
    members: Mapped[list["ChannelMember"]] = relationship(
        "ChannelMember", back_populates="channel", cascade="all, delete-orphan",
    )
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="channel", cascade="all, delete-orphan",
    )
    creator: Mapped["User"] = relationship("User", foreign_keys=[created_by])
    voice_messages: Mapped[list["VoiceMessage"]] = relationship(
        "VoiceMessage", back_populates="channel", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Channel {self.type}:{self.name or 'DM'} ({self.id[:8]})>"


class ChannelMember(Base, TimestampMixin):
    __tablename__ = "channel_members"
    __table_args__ = (
        UniqueConstraint("channel_id", "user_id", name="uq_channel_member"),
    )

    channel_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="member",
    )  # "admin", "moderator", "member"
    last_read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_read_message_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    is_muted: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Auto-unmute time. NULL = muted indefinitely (when is_muted=True).
    mute_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Per-user archive flag — hides channel from main list without leaving
    is_archived: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_pinned: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Group ban (audit fix 1.4). Permanent if banned_at IS NOT NULL and
    # banned_until IS NULL. Otherwise the ban auto-expires at
    # banned_until. Sender check in MessageService.send_message refuses
    # any message where (banned_at IS NOT NULL) AND
    # (banned_until IS NULL OR banned_until > now()).
    banned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    banned_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    banned_by: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False,
    )

    # Relationships
    channel: Mapped["Channel"] = relationship("Channel", back_populates="members")
    user: Mapped["User"] = relationship("User")

    def __repr__(self) -> str:
        return f"<ChannelMember ch={self.channel_id[:8]} user={self.user_id[:8]}>"
