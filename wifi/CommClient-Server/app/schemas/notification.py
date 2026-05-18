"""
Notification schemas for REST API and internal use.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class NotificationResponse(BaseModel):
    """Single notification with all details."""

    id: str
    type: str
    title: str
    body: str | None
    reference_id: str | None
    reference_type: str | None
    is_read: bool
    read_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class NotificationListResponse(BaseModel):
    """Paginated list of notifications with metadata."""

    notifications: list[NotificationResponse]
    total: int
    unread_count: int


class NotificationCreate(BaseModel):
    """Internal schema for creating notifications — not exposed in REST API."""

    type: str = Field(
        ...,
        pattern=r"^(message|call_missed|call_incoming|contact_request|group_invite|system|mention)$",
    )
    title: str = Field(..., min_length=1, max_length=256)
    body: str | None = Field(None, max_length=5000)
    reference_id: str | None = Field(None, max_length=32)
    reference_type: str | None = Field(
        None,
        pattern=r"^(message|call|channel|user|contact)$",
    )


class MarkReadRequest(BaseModel):
    """Request body for marking notifications as read."""

    notification_ids: list[str] = Field(..., min_length=1, max_length=100)
