"""
Media gallery Pydantic schemas — request/response models with filtering.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MediaItemResponse(BaseModel):
    """Single media item metadata."""
    id: str
    channel_id: str
    message_id: str | None
    uploader_id: str
    filename: str
    mime_type: str
    file_size: int
    width: int | None
    height: int | None
    duration_ms: int | None
    media_type: Literal["image", "video", "audio", "document"]
    is_favorite: bool
    thumbnail_url: str | None
    download_url: str
    created_at: datetime

    class Config:
        from_attributes = True


class MediaItemListResponse(BaseModel):
    """Paginated media list with filtering."""
    items: list[MediaItemResponse]
    total: int
    page: int
    per_page: int


class MediaAlbumItemResponse(BaseModel):
    """Media item in album context."""
    id: str
    filename: str
    mime_type: str
    media_type: Literal["image", "video", "audio", "document"]
    thumbnail_url: str | None
    download_url: str

    class Config:
        from_attributes = True


class MediaAlbumResponse(BaseModel):
    """Album with its items."""
    id: str
    channel_id: str
    name: str
    description: str | None
    cover_item_id: str | None
    cover_item: MediaAlbumItemResponse | None
    created_by: str
    created_at: datetime
    items: list[MediaAlbumItemResponse] = []

    class Config:
        from_attributes = True


class MediaAlbumListResponse(BaseModel):
    """List of albums in a channel."""
    albums: list[MediaAlbumResponse]
    total: int


class MediaAlbumCreate(BaseModel):
    """Create album request."""
    name: str = Field(..., min_length=1, max_length=256)
    description: str | None = Field(None, max_length=2000)


class MediaAlbumAddItems(BaseModel):
    """Add items to album."""
    item_ids: list[str] = Field(..., min_items=1)


class MediaChannelStats(BaseModel):
    """Media statistics for a channel."""
    channel_id: str
    total_items: int
    by_type: dict[str, int]  # {"image": 10, "video": 5, ...}
    total_size_bytes: int
    total_albums: int
    favorites_count: int


class MediaItemFavoriteToggle(BaseModel):
    """Toggle favorite response."""
    item_id: str
    is_favorite: bool
