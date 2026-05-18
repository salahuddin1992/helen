"""
Media gallery service — indexing, querying, and managing shared media.
Generates thumbnails for images/videos and deduplicates by checksum.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiofiles
from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.media_gallery import MediaItem, MediaAlbum, MediaAlbumItem
from app.models.message import Message

logger = get_logger(__name__)
settings = get_settings()


class MediaGalleryService:
    """Service for media indexing, querying, and album management."""

    @staticmethod
    async def index_media(
        db: AsyncSession,
        channel_id: str,
        file_path: str,
        uploader_id: str,
        filename: str,
        mime_type: str,
        file_size: int,
        message_id: str | None = None,
        media_type: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
    ) -> MediaItem:
        """
        Index a media file. Generate thumbnail for images/videos.
        Deduplicates by checksum.
        """
        # Compute checksum for deduplication
        checksum = await MediaGalleryService.compute_checksum(file_path)

        # Check for duplicate
        result = await db.execute(
            select(MediaItem).where(MediaItem.checksum == checksum)
        )
        existing = result.scalar_one_or_none()
        if existing:
            logger.info("media_deduplicated", checksum=checksum, media_id=existing.id)
            return existing

        # Infer media type if not provided
        if not media_type:
            media_type = MediaGalleryService._infer_media_type(mime_type)

        # Generate thumbnail
        thumbnail_path = None
        if media_type in ("image", "video"):
            try:
                thumbnail_path = await MediaGalleryService.generate_thumbnail(
                    file_path, mime_type, media_type
                )
            except Exception as e:
                logger.warning("thumbnail_generation_failed", error=str(e), file=filename)

        item = MediaItem(
            channel_id=channel_id,
            message_id=message_id,
            uploader_id=uploader_id,
            file_path=file_path,
            filename=filename,
            mime_type=mime_type,
            file_size=file_size,
            width=width,
            height=height,
            duration_ms=duration_ms,
            thumbnail_path=str(thumbnail_path) if thumbnail_path else None,
            media_type=media_type,
            checksum=checksum,
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)

        logger.info(
            "media_indexed",
            media_id=item.id,
            channel_id=channel_id,
            filename=filename,
            media_type=media_type,
        )
        return item

    @staticmethod
    async def get_media(
        db: AsyncSession,
        channel_id: str,
        media_type: str | None = None,
        uploader_id: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        is_favorite: bool | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[MediaItem], int]:
        """
        Paginated media query with filters.
        Returns (items, total_count).
        """
        query = select(MediaItem).where(MediaItem.channel_id == channel_id)

        if media_type:
            query = query.where(MediaItem.media_type == media_type)
        if uploader_id:
            query = query.where(MediaItem.uploader_id == uploader_id)
        if from_date:
            query = query.where(MediaItem.created_at >= from_date)
        if to_date:
            query = query.where(MediaItem.created_at <= to_date)
        if is_favorite is not None:
            query = query.where(MediaItem.is_favorite == is_favorite)

        # Count total
        count_result = await db.execute(
            select(func.count(MediaItem.id)).select_from(MediaItem)
            .where(MediaItem.channel_id == channel_id)
        )
        total = count_result.scalar() or 0

        # Apply pagination and ordering
        query = query.order_by(desc(MediaItem.created_at)).offset(
            (page - 1) * per_page
        ).limit(per_page)

        result = await db.execute(query)
        items = result.scalars().all()

        return items, total

    @staticmethod
    async def get_media_item(db: AsyncSession, item_id: str) -> MediaItem:
        """Get single media item with metadata."""
        result = await db.execute(
            select(MediaItem).where(MediaItem.id == item_id)
        )
        item = result.scalar_one_or_none()
        if not item:
            raise NotFoundError("MediaItem", item_id)
        return item

    @staticmethod
    async def toggle_favorite(
        db: AsyncSession, item_id: str, user_id: str
    ) -> MediaItem:
        """Toggle favorite status for a media item."""
        item = await MediaGalleryService.get_media_item(db, item_id)

        # Verify user has access (uploader or channel member can favorite)
        # This is enforced at route level

        item.is_favorite = not item.is_favorite
        await db.commit()
        await db.refresh(item)

        logger.info(
            "media_favorite_toggled",
            media_id=item_id,
            user_id=user_id,
            is_favorite=item.is_favorite,
        )
        return item

    @staticmethod
    async def create_album(
        db: AsyncSession, channel_id: str, user_id: str, name: str, description: str | None = None
    ) -> MediaAlbum:
        """Create a new album in a channel."""
        album = MediaAlbum(
            channel_id=channel_id,
            name=name,
            description=description,
            created_by=user_id,
        )
        db.add(album)
        await db.commit()
        await db.refresh(album)

        logger.info(
            "media_album_created",
            album_id=album.id,
            channel_id=channel_id,
            name=name,
        )
        return album

    @staticmethod
    async def add_to_album(
        db: AsyncSession, album_id: str, item_ids: list[str]
    ) -> MediaAlbum:
        """Add items to album. Silently skips duplicates."""
        album = await db.execute(
            select(MediaAlbum).where(MediaAlbum.id == album_id)
        )
        album = album.scalar_one_or_none()
        if not album:
            raise NotFoundError("MediaAlbum", album_id)

        # Get existing item IDs to skip duplicates
        existing = await db.execute(
            select(MediaAlbumItem.media_item_id).where(
                MediaAlbumItem.album_id == album_id
            )
        )
        existing_ids = {row[0] for row in existing.fetchall()}

        # Add new items
        added = 0
        for item_id in item_ids:
            if item_id not in existing_ids:
                item_assoc = MediaAlbumItem(album_id=album_id, media_item_id=item_id)
                db.add(item_assoc)
                added += 1

        if added > 0:
            await db.commit()

        # Re-fetch with items + media_item eagerly loaded so the route can
        # serialize without triggering lazy I/O.
        result = await db.execute(
            select(MediaAlbum)
            .where(MediaAlbum.id == album_id)
            .options(
                selectinload(MediaAlbum.items).selectinload(MediaAlbumItem.media_item)
            )
        )
        album = result.scalar_one()
        logger.info("media_album_items_added", album_id=album_id, count=added)
        return album

    @staticmethod
    async def get_album(db: AsyncSession, album_id: str) -> MediaAlbum:
        """Get album with all its items (and each item's media_item eager-loaded)."""
        result = await db.execute(
            select(MediaAlbum)
            .where(MediaAlbum.id == album_id)
            .options(
                selectinload(MediaAlbum.items).selectinload(MediaAlbumItem.media_item)
            )
        )
        album = result.scalar_one_or_none()
        if not album:
            raise NotFoundError("MediaAlbum", album_id)
        return album

    @staticmethod
    async def get_channel_albums(db: AsyncSession, channel_id: str) -> list[MediaAlbum]:
        """List all albums in a channel (with items + media_item eager-loaded)."""
        result = await db.execute(
            select(MediaAlbum)
            .where(MediaAlbum.channel_id == channel_id)
            .options(
                selectinload(MediaAlbum.items).selectinload(MediaAlbumItem.media_item)
            )
            .order_by(desc(MediaAlbum.created_at))
        )
        return result.scalars().all()

    @staticmethod
    async def get_channel_stats(db: AsyncSession, channel_id: str) -> dict:
        """
        Get media statistics for a channel.
        Returns counts by type, total size, album count, favorites.
        """
        # Total items
        total_result = await db.execute(
            select(func.count(MediaItem.id)).where(MediaItem.channel_id == channel_id)
        )
        total_items = total_result.scalar() or 0

        # By type
        type_result = await db.execute(
            select(MediaItem.media_type, func.count(MediaItem.id))
            .where(MediaItem.channel_id == channel_id)
            .group_by(MediaItem.media_type)
        )
        by_type = {row[0]: row[1] for row in type_result.fetchall()}

        # Total size
        size_result = await db.execute(
            select(func.sum(MediaItem.file_size)).where(
                MediaItem.channel_id == channel_id
            )
        )
        total_size_bytes = size_result.scalar() or 0

        # Albums
        albums_result = await db.execute(
            select(func.count(MediaAlbum.id)).where(
                MediaAlbum.channel_id == channel_id
            )
        )
        total_albums = albums_result.scalar() or 0

        # Favorites
        favorites_result = await db.execute(
            select(func.count(MediaItem.id)).where(
                and_(
                    MediaItem.channel_id == channel_id,
                    MediaItem.is_favorite == True,
                )
            )
        )
        favorites_count = favorites_result.scalar() or 0

        return {
            "total_items": total_items,
            "by_type": by_type,
            "total_size_bytes": total_size_bytes,
            "total_albums": total_albums,
            "favorites_count": favorites_count,
        }

    @staticmethod
    async def generate_thumbnail(
        file_path: str, mime_type: str, media_type: str
    ) -> Path | None:
        """
        Generate thumbnail for images (Pillow) or videos (frame extraction).
        Returns thumbnail path or None on failure.
        """
        try:
            if media_type == "image":
                return await MediaGalleryService._generate_image_thumbnail(file_path)
            elif media_type == "video":
                return await MediaGalleryService._generate_video_thumbnail(file_path)
        except Exception as e:
            logger.warning("thumbnail_generation_error", file=file_path, error=str(e))
            return None
        return None

    @staticmethod
    async def _generate_image_thumbnail(file_path: str) -> Path | None:
        """Generate an image thumbnail (delegates to FileService for consistency)."""
        from app.services.file_service import FileService

        source = Path(file_path)
        # Write the thumb next to the source so it lives with the album
        return await FileService._generate_image_thumbnail(
            source, source.stem, output_dir=source.parent
        )

    @staticmethod
    async def _generate_video_thumbnail(file_path: str) -> Path | None:
        """Generate a video thumbnail (delegates to FileService — uses ffmpeg + ffprobe)."""
        from app.services.file_service import FileService

        source = Path(file_path)
        return await FileService._generate_video_thumbnail(
            source, source.stem, output_dir=source.parent
        )

    @staticmethod
    async def compute_checksum(file_path: str) -> str:
        """Compute SHA256 checksum of file for deduplication."""
        sha256 = hashlib.sha256()
        async with aiofiles.open(file_path, "rb") as f:
            async for chunk in f.iter_chunked(8192):
                sha256.update(chunk)
        return sha256.hexdigest()

    @staticmethod
    def _infer_media_type(mime_type: str) -> str:
        """Infer media type from MIME type."""
        if mime_type.startswith("image/"):
            return "image"
        elif mime_type.startswith("video/"):
            return "video"
        elif mime_type.startswith("audio/"):
            return "audio"
        else:
            return "document"
