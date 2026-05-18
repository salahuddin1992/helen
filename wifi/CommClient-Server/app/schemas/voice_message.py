"""
Voice message request/response schemas.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class VoiceMessageCreate(BaseModel):
    """Voice message upload request (multipart form)."""

    duration_ms: int = Field(..., ge=0, le=3600000, description="Duration in milliseconds (max 1 hour)")
    # Audio file uploaded as multipart FormData with key="file"


class VoiceMessageResponse(BaseModel):
    """Voice message metadata response."""

    id: str
    channel_id: str
    sender_id: str
    sender_username: str | None = None
    duration_ms: int = Field(..., description="Duration in milliseconds")
    file_size: int = Field(..., description="File size in bytes")
    mime_type: str = Field(..., description="MIME type (audio/mpeg, etc.)")
    waveform_data: list[float] | None = Field(
        None, description="Normalized peak amplitude samples (0-1.0)"
    )
    transcription: str | None = Field(None, description="Optional transcribed text")
    is_read: bool = Field(False, description="Whether message has been played")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class VoiceMessageListResponse(BaseModel):
    """Voice message list response with pagination."""

    messages: list[VoiceMessageResponse]
    total: int = Field(..., description="Total messages in channel")
    has_more: bool = Field(..., description="Whether more results available")
    limit: int = Field(..., description="Query limit")


class VoiceMessageUpdate(BaseModel):
    """Update voice message (mark as read, add transcription)."""

    is_read: bool | None = None
    transcription: str | None = Field(None, max_length=10000, description="Transcribed text")


class WaveformDataRequest(BaseModel):
    """Request to regenerate waveform data."""

    samples: int = Field(100, ge=10, le=1000, description="Number of samples to generate")


class WaveformDataResponse(BaseModel):
    """Waveform data response."""

    samples: int = Field(..., description="Number of samples")
    data: list[float] = Field(..., description="Normalized peak amplitudes (0-1.0)")
    duration_ms: int = Field(..., description="Audio duration in milliseconds")
