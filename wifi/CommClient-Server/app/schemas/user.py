"""
User and contact schemas.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    id: str
    username: str
    share_code: str
    display_name: str
    avatar_url: str | None
    bio: str | None
    status: str
    status_message: str | None = None
    status_expires_at: datetime | None = None
    last_seen: datetime
    created_at: datetime
    role: str = "user"

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=128)
    avatar_url: str | None = None
    bio: str | None = Field(None, max_length=500)
    status: str | None = Field(None, pattern=r"^(online|offline|away|busy|dnd)$")
    status_message: str | None = Field(None, max_length=140)
    # Optional ISO datetime when the status_message should auto-clear.
    # Pass null/None or omit to clear the expiry.
    status_expires_at: datetime | None = None


class StatusMessageUpdate(BaseModel):
    """Dedicated payload for setting only the custom status message."""
    status_message: str | None = Field(None, max_length=140)
    status_expires_at: datetime | None = None


class UserListResponse(BaseModel):
    users: list[UserProfile]
    total: int


class ContactCreate(BaseModel):
    contact_id: str
    nickname: str | None = Field(None, max_length=128)


class ContactUpdate(BaseModel):
    nickname: str | None = Field(None, max_length=128)
    is_blocked: bool | None = None
    is_favorite: bool | None = None


class ContactResponse(BaseModel):
    id: str
    contact: UserProfile
    nickname: str | None
    is_blocked: bool
    is_favorite: bool
    created_at: datetime

    class Config:
        from_attributes = True
