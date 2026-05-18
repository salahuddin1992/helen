"""
User model — local authentication, profile, presence.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.share_code import generate_share_code
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # Public 64-char alphanumeric code users share to be found by others.
    # Generated at row-insert time; unique index enforces no collisions.
    share_code: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        default=generate_share_code,
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    bio: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="offline",
    )  # online, offline, away, busy, dnd, in_call
    # User-defined custom status message (e.g. "📅 In a meeting", "🏖️ On vacation")
    status_message: Mapped[str | None] = mapped_column(String(140), nullable=True)
    # ISO timestamp when status_message expires (auto-clears). NULL = no expiry.
    status_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    # ── RBAC ───────────────────────────────────────────────
    # Roles: "user" (default), "moderator", "admin"
    # First registered user is auto-promoted to "admin"
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user",
    )

    # ── Camera quality ─────────────────────────────────────
    # User's quick-pick camera preset. NULL = "use server default" (the
    # CameraQualityPreset row with is_default=True). ON DELETE SET NULL
    # so that removing/disabling a preset doesn't orphan a user — they
    # just fall back to the default.
    active_camera_preset_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("camera_quality_presets.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships
    sessions: Mapped[list["UserSession"]] = relationship(
        "UserSession", back_populates="user", cascade="all, delete-orphan",
    )
    sent_messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="sender", foreign_keys="Message.sender_id",
    )
    notifications: Mapped[list["Notification"]] = relationship(
        "Notification", back_populates="user", cascade="all, delete-orphan",
    )
    voice_messages: Mapped[list["VoiceMessage"]] = relationship(
        "VoiceMessage", back_populates="sender", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.id[:8]})>"
