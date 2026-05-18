"""
Device token model — registered push notification endpoints per user.

Each user can have multiple devices (phone, tablet, desktop). When a
notification is created we look up active tokens for the recipient and
dispatch via the matching provider (FCM for Android/web, APNs for iOS).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DeviceToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A push notification endpoint registered by a client device."""

    __tablename__ = "device_tokens"
    __table_args__ = (
        UniqueConstraint("provider", "token", name="uq_device_token_provider_token"),
        Index("ix_device_tokens_user_id_active", "user_id", "is_active"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "fcm" | "apns" | "web"
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    # Opaque device token / registration id from the platform
    token: Mapped[str] = mapped_column(String(512), nullable=False)
    # "ios" | "android" | "web" | "desktop"
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    # Optional human-readable name (e.g. "Pixel 8", "Chrome on macOS")
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # APNs needs to know whether to use the .voip topic, etc.
    bundle_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Web push needs the keys (p256dh + auth) — stored opaque/JSON
    extra_json: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Number of consecutive delivery failures — token is disabled after a threshold
    failure_count: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(256), nullable=True)

    user: Mapped["User"] = relationship("User")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<DeviceToken {self.id[:8]} provider={self.provider} "
            f"user={self.user_id[:8]} active={self.is_active}>"
        )
