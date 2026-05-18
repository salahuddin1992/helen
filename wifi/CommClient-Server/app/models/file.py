"""
File metadata model — tracks uploaded files and thumbnails.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FileRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "files"

    uploader_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    channel_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("channels.id", ondelete="SET NULL"), nullable=True,
    )
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Relationships
    uploader: Mapped["User"] = relationship("User", foreign_keys=[uploader_id])

    def __repr__(self) -> str:
        return f"<FileRecord {self.original_name} ({self.id[:8]})>"
