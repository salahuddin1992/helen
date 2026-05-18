"""
File upload/download schemas.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class FileResponse(BaseModel):
    id: str
    original_name: str
    mime_type: str
    size_bytes: int
    thumbnail_url: str | None
    download_url: str
    uploader_id: str
    created_at: datetime

    class Config:
        from_attributes = True


class FileListResponse(BaseModel):
    files: list[FileResponse]
    total: int
