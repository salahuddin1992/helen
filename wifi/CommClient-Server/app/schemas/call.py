"""
Call signaling and call log schemas.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CallLogResponse(BaseModel):
    id: str
    channel_id: str | None
    initiator_id: str
    call_type: str
    routing: str
    status: str
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    participant_count: int
    created_at: datetime

    class Config:
        from_attributes = True


class CallLogListResponse(BaseModel):
    calls: list[CallLogResponse]
    total: int


class CallInitiate(BaseModel):
    """Socket event payload for initiating a call."""
    callee_id: str | None = None  # For 1-to-1
    channel_id: str | None = None  # For group
    media_type: str = Field(..., pattern=r"^(audio|video)$")


class CallSignal(BaseModel):
    """Socket event payload for WebRTC signaling."""
    target_id: str
    sdp: dict | None = None
    candidate: dict | None = None
