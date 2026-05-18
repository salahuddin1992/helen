"""
Media gallery models — indexing and organizing shared media per conversation.
Supports images, videos, audio, documents with thumbnails and favorites.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MediaItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Individual media file indexed in a channel."""
    __tablename__ = "media_items"

    channel_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    message_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    uploader_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int | None] = mapped_column(nullable=True)  # For images/videos
    height: Mapped[int | None] = mapped_column(nullable=True)  # For images/videos
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)  # For audio/video
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str] = mapped_column(
        String(16), nullable=False,
    )  # "image", "video", "audio", "document"
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    is_favorite: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)

    # Relationships
    channel: Mapped["Channel"] = relationship("Channel", foreign_keys=[channel_id])
    message: Mapped["Message | None"] = relationship("Message", foreign_keys=[message_id])
    uploader: Mapped["User"] = relationship("User", foreign_keys=[uploader_id])
    album_items: Mapped[list["MediaAlbumItem"]] = relationship(
        "MediaAlbumItem", back_populates="media_item", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<MediaItem {self.filename} ({self.id[:8]})>"


class MediaAlbum(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """User-created albums to organize media within a channel."""
    __tablename__ = "media_albums"

    channel_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_item_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True,
    )
    created_by: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )

    # Relationships
    channel: Mapped["Channel"] = relationship("Channel", foreign_keys=[channel_id])
    cover_item: Mapped["MediaItem | None"] = relationship(
        "MediaItem", foreign_keys=[cover_item_id],
    )
    creator: Mapped["User"] = relationship("User", foreign_keys=[created_by])
    items: Mapped[list["MediaAlbumItem"]] = relationship(
        "MediaAlbumItem", back_populates="album", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<MediaAlbum {self.name} ({self.id[:8]})>"


class MediaAlbumItem(Base, TimestampMixin):
    """Join table linking media items to albums."""
    __tablename__ = "media_album_items"
    __table_args__ = (
        UniqueConstraint("album_id", "media_item_id", name="uq_album_item"),
    )

    album_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("media_albums.id", ondelete="CASCADE"), primary_key=True,
    )
    media_item_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("media_items.id", ondelete="CASCADE"), primary_key=True,
    )

    # Relationships
    album: Mapped["MediaAlbum"] = relationship("MediaAlbum", back_populates="items")
    media_item: Mapped["MediaItem"] = relationship("MediaItem", back_populates="album_items")

    def __repr__(self) -> str:
        return f"<MediaAlbumItem album={self.album_id[:8]} item={self.media_item_id[:8]}>"
