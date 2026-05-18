"""
Media gallery REST endpoints — indexing, querying, albums, favorites.

Hardened:
  - All routes require channel membership authorization
  - Filtering by uploader_id only returns current user's data
  - Media access checks channel membership
"""

from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_permission_denied
from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.schemas.media_gallery import (
    MediaAlbumAddItems,
    MediaAlbumCreate,
    MediaAlbumListResponse,
    MediaAlbumResponse,
    MediaChannelStats,
    MediaItemListResponse,
    MediaItemResponse,
    MediaItemFavoriteToggle,
)
from app.services.channel_service import ChannelService
from app.services.media_gallery_service import MediaGalleryService

logger = get_logger(__name__)
router = APIRouter(prefix="/media", tags=["media"])


async def _verify_channel_access(
    db: AsyncSession, user_id: str, channel_id: str
) -> None:
    """Verify user is a channel member."""
    is_member = await ChannelService.is_member(db, channel_id, user_id)
    if not is_member:
        audit_permission_denied(user_id, f"channel:{channel_id}", "media_access")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a channel member to access media",
        )


@router.get("/channel/{channel_id}", response_model=MediaItemListResponse)
async def list_channel_media(
    channel_id: str,
    media_type: str | None = Query(None, enum=["image", "video", "audio", "document"]),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    uploader_id: str | None = Query(None),
    is_favorite: bool | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    List media in channel with filtering.
    Query params: media_type, from_date (ISO), to_date (ISO), uploader_id, is_favorite, page, per_page.
    """
    await _verify_channel_access(db, user_id, channel_id)

    # Parse dates
    from_dt = None
    to_dt = None
    if from_date:
        try:
            from_dt = datetime.fromisoformat(from_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="from_date must be ISO format",
            )
    if to_date:
        try:
            to_dt = datetime.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="to_date must be ISO format",
            )

    # Verify uploader_id is either current user or not specified
    if uploader_id and uploader_id != user_id:
        audit_permission_denied(user_id, f"user:{uploader_id}", "filter_media")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only filter by your own uploads",
        )

    items, total = await MediaGalleryService.get_media(
        db,
        channel_id=channel_id,
        media_type=media_type,
        uploader_id=uploader_id or user_id,
        from_date=from_dt,
        to_date=to_dt,
        is_favorite=is_favorite,
        page=page,
        per_page=per_page,
    )

    return MediaItemListResponse(
        items=[
            MediaItemResponse(
                **{
                    **{c.name: getattr(item, c.name) for c in item.__table__.columns},
                    "thumbnail_url": f"/api/media/{item.id}/thumbnail"
                    if item.thumbnail_path
                    else None,
                    "download_url": f"/api/media/{item.id}",
                }
            )
            for item in items
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/{item_id}", response_model=MediaItemResponse)
async def get_media_item(
    item_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get media item metadata."""
    item = await MediaGalleryService.get_media_item(db, item_id)

    # Verify channel access
    await _verify_channel_access(db, user_id, item.channel_id)

    return MediaItemResponse(
        **{
            **{c.name: getattr(item, c.name) for c in item.__table__.columns},
            "thumbnail_url": f"/api/media/{item.id}/thumbnail"
            if item.thumbnail_path
            else None,
            "download_url": f"/api/media/{item.id}",
        }
    )


@router.get("/{item_id}/thumbnail")
async def get_media_thumbnail(
    item_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Serve media thumbnail."""
    item = await MediaGalleryService.get_media_item(db, item_id)

    # Verify channel access
    await _verify_channel_access(db, user_id, item.channel_id)

    if not item.thumbnail_path or not os.path.exists(item.thumbnail_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thumbnail not available",
        )

    return FileResponse(
        item.thumbnail_path,
        media_type="image/jpeg",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/{item_id}/download")
async def download_media(
    item_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Download media file."""
    item = await MediaGalleryService.get_media_item(db, item_id)

    # Verify channel access
    await _verify_channel_access(db, user_id, item.channel_id)

    if not os.path.exists(item.file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    logger.info("media_downloaded", item_id=item_id, user_id=user_id)

    return FileResponse(
        item.file_path,
        media_type=item.mime_type,
        filename=item.filename,
        headers={
            "Content-Disposition": f'attachment; filename="{item.filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{item_id}/favorite", response_model=MediaItemFavoriteToggle, status_code=200)
async def toggle_favorite(
    item_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Toggle favorite status for media item."""
    item = await MediaGalleryService.get_media_item(db, item_id)

    # Verify channel access
    await _verify_channel_access(db, user_id, item.channel_id)

    item = await MediaGalleryService.toggle_favorite(db, item_id, user_id)

    return MediaItemFavoriteToggle(item_id=item.id, is_favorite=item.is_favorite)


@router.post("/albums", response_model=MediaAlbumResponse, status_code=201)
async def create_album(
    channel_id: str = Query(...),
    req: MediaAlbumCreate = ...,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Create new album in channel."""
    await _verify_channel_access(db, user_id, channel_id)

    album = await MediaGalleryService.create_album(
        db, channel_id=channel_id, user_id=user_id, name=req.name, description=req.description
    )

    return MediaAlbumResponse(
        **{c.name: getattr(album, c.name) for c in album.__table__.columns},
        cover_item=None,
        items=[],
    )


@router.get("/albums/channel/{channel_id}", response_model=MediaAlbumListResponse)
async def list_channel_albums(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """List all albums in channel."""
    await _verify_channel_access(db, user_id, channel_id)

    albums = await MediaGalleryService.get_channel_albums(db, channel_id)

    return MediaAlbumListResponse(
        albums=[
            MediaAlbumResponse(
                **{c.name: getattr(album, c.name) for c in album.__table__.columns},
                cover_item=None,
                items=[
                    {
                        "id": item.media_item.id,
                        "filename": item.media_item.filename,
                        "mime_type": item.media_item.mime_type,
                        "media_type": item.media_item.media_type,
                        "thumbnail_url": f"/api/media/{item.media_item.id}/thumbnail"
                        if item.media_item.thumbnail_path
                        else None,
                        "download_url": f"/api/media/{item.media_item.id}",
                    }
                    for item in album.items
                ],
            )
            for album in albums
        ],
        total=len(albums),
    )


@router.get("/albums/{album_id}", response_model=MediaAlbumResponse)
async def get_album(
    album_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get album with all its items."""
    album = await MediaGalleryService.get_album(db, album_id)

    # Verify channel access
    await _verify_channel_access(db, user_id, album.channel_id)

    return MediaAlbumResponse(
        **{c.name: getattr(album, c.name) for c in album.__table__.columns},
        cover_item=None,
        items=[
            {
                "id": item.media_item.id,
                "filename": item.media_item.filename,
                "mime_type": item.media_item.mime_type,
                "media_type": item.media_item.media_type,
                "thumbnail_url": f"/api/media/{item.media_item.id}/thumbnail"
                if item.media_item.thumbnail_path
                else None,
                "download_url": f"/api/media/{item.media_item.id}",
            }
            for item in album.items
        ],
    )


@router.post("/albums/{album_id}/items", response_model=MediaAlbumResponse)
async def add_items_to_album(
    album_id: str,
    req: MediaAlbumAddItems,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Add items to album."""
    album = await MediaGalleryService.get_album(db, album_id)

    # Verify channel access
    await _verify_channel_access(db, user_id, album.channel_id)

    album = await MediaGalleryService.add_to_album(db, album_id, req.item_ids)

    return MediaAlbumResponse(
        **{c.name: getattr(album, c.name) for c in album.__table__.columns},
        cover_item=None,
        items=[
            {
                "id": item.media_item.id,
                "filename": item.media_item.filename,
                "mime_type": item.media_item.mime_type,
                "media_type": item.media_item.media_type,
                "thumbnail_url": f"/api/media/{item.media_item.id}/thumbnail"
                if item.media_item.thumbnail_path
                else None,
                "download_url": f"/api/media/{item.media_item.id}",
            }
            for item in album.items
        ],
    )


@router.get("/channel/{channel_id}/stats", response_model=MediaChannelStats)
async def get_channel_stats(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get media statistics for channel."""
    await _verify_channel_access(db, user_id, channel_id)

    stats = await MediaGalleryService.get_channel_stats(db, channel_id)
    return MediaChannelStats(channel_id=channel_id, **stats)
