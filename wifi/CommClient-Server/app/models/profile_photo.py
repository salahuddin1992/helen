"""
Profile photo model — Telegram-style multi-photo profile with per-photo visibility.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProfilePhoto(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "profile_photos"
    __table_args__ = (
        Index("ix_profile_photos_user_position", "user_id", "position"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Filename inside <upload_path>/profile_photos/<user_id>/ — never a full path.
    storage_name: Mapped[str] = mapped_column(String(128), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # "public" | "contacts" | "private"
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="public", server_default="public",
    )
    # Exactly one photo per user should be is_primary=True; enforced in service layer.
    is_primary: Mapped[bool] = mapped_column(
        default=False, nullable=False, server_default="0",
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<ProfilePhoto {self.id[:8]} user={self.user_id[:8]} vis={self.visibility}>"
