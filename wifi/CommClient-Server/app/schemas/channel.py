"""
Channel / room schemas.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ChannelCreate(BaseModel):
    type: str = Field(..., pattern=r"^(dm|group)$")
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = Field(None, max_length=1000)
    member_ids: list[str] = Field(default_factory=list)


class ChannelUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = Field(None, max_length=1000)
    avatar_url: str | None = None


class ChannelMemberInfo(BaseModel):
    user_id: str
    username: str
    display_name: str
    avatar_url: str | None
    status: str
    role: str
    joined_at: datetime


class ChannelResponse(BaseModel):
    id: str
    type: str
    name: str | None
    description: str | None
    avatar_url: str | None
    created_by: str | None
    is_active: bool
    members: list[ChannelMemberInfo]
    member_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ChannelListResponse(BaseModel):
    channels: list[ChannelResponse]
    total: int


class AddMemberRequest(BaseModel):
    user_id: str
    role: str = Field("member", pattern=r"^(admin|moderator|member)$")


class ChannelBrief(BaseModel):
    id: str
    type: str
    name: str | None
    member_count: int
    last_message_at: datetime | None = None

    class Config:
        from_attributes = True


# ── Per-user channel preferences ─────────────────────────

class ChannelArchiveRequest(BaseModel):
    archived: bool


class ChannelPinRequest(BaseModel):
    pinned: bool


class ChannelMuteRequest(BaseModel):
    muted: bool
    mute_until: datetime | None = None


class ChannelReadRequest(BaseModel):
    message_id: str | None = None


class ChannelMemberPrefsResponse(BaseModel):
    channel_id: str
    user_id: str
    is_archived: bool
    is_pinned: bool
    is_muted: bool
    mute_until: datetime | None
    last_read_at: datetime | None
    last_read_message_id: str | None

    class Config:
        from_attributes = True
