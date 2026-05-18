"""
File drop Pydantic schemas — chunked transfers, progress, shared folders.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FileTransferInit(BaseModel):
    """Initialize file transfer."""
    filename: str = Field(..., min_length=1, max_length=512)
    file_size: int = Field(..., gt=0)
    mime_type: str = Field(default="application/octet-stream", max_length=128)
    checksum: str = Field(..., min_length=1, max_length=64)
    receiver_id: str | None = None  # For DM
    channel_id: str | None = None  # For group


class FileTransferChunk(BaseModel):
    """Upload a single chunk."""
    chunk_index: int = Field(..., ge=0)
    chunk_data: bytes


class FileTransferStatus(BaseModel):
    """File transfer status."""
    id: str
    filename: str
    file_size: int
    status: Literal["pending", "uploading", "completed", "failed", "cancelled"]
    total_chunks: int
    received_chunks: int
    progress_percent: float
    speed_bps: float | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class FileTransferResponse(BaseModel):
    """Complete file transfer info."""
    id: str
    filename: str
    file_size: int
    mime_type: str
    status: Literal["pending", "uploading", "completed", "failed", "cancelled"]
    total_chunks: int
    received_chunks: int
    sender_id: str
    receiver_id: str | None
    channel_id: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class FileTransferListResponse(BaseModel):
    """List of active transfers."""
    transfers: list[FileTransferResponse]
    total: int


class SharedFolderCreate(BaseModel):
    """Create shared folder."""
    name: str = Field(default="Shared Files", min_length=1, max_length=256)
    max_size_bytes: int = Field(default=1 * 1024 * 1024 * 1024, ge=1024 * 1024)


class SharedFolderFile(BaseModel):
    """File in shared folder."""
    id: str
    filename: str
    mime_type: str
    file_size: int
    path_in_folder: str
    added_by: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class SharedFolderResponse(BaseModel):
    """Shared folder info with files."""
    id: str
    channel_id: str
    name: str
    created_by: str | None
    current_size_bytes: int
    max_size_bytes: int
    files: list[SharedFolderFile] = []
    created_at: datetime

    class Config:
        from_attributes = True


class SharedFolderAddFile(BaseModel):
    """Add file to shared folder."""
    file_id: str = Field(...)
    path_in_folder: str = Field(..., min_length=1, max_length=512)


class FileTransferOffer(BaseModel):
    """Offer file transfer to recipient."""
    filename: str
    file_size: int
    mime_type: str


class FileTransferOfferAccept(BaseModel):
    """Accept file offer."""
    transfer_id: str
