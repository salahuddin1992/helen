"""Profile photo schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


Visibility = Literal["public", "contacts", "private"]


class ProfilePhotoResponse(BaseModel):
    id: str
    user_id: str
    visibility: Visibility
    is_primary: bool
    position: int
    mime_type: str
    size_bytes: int
    caption: str | None = None
    # Full relative URL the client should use to fetch the binary.
    url: str
    created_at: datetime

    class Config:
        from_attributes = True


class ProfilePhotoListResponse(BaseModel):
    photos: list[ProfilePhotoResponse]
    total: int


class ProfilePhotoUpdate(BaseModel):
    visibility: Visibility | None = None
    is_primary: bool | None = None
    caption: str | None = Field(None, max_length=500)
    position: int | None = Field(None, ge=0)
