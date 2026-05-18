"""
Message schemas.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)
    type: str = Field("text", pattern=r"^(text|file|image|reply)$")
    reply_to: str | None = None
    file_id: str | None = None


class MessageUpdate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)


class ReactionCreate(BaseModel):
    emoji: str = Field(..., min_length=1, max_length=32)


class SenderBrief(BaseModel):
    id: str
    username: str
    display_name: str
    avatar_url: str | None

    class Config:
        from_attributes = True


class MessageResponse(BaseModel):
    id: str
    channel_id: str
    sender: SenderBrief
    content: str
    type: str
    reply_to: str | None
    file_id: str | None
    status: str
    reactions: list["ReactionInfo"]
    edited_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class ReactionInfo(BaseModel):
    emoji: str
    count: int
    user_ids: list[str]


class MessageListResponse(BaseModel):
    messages: list[MessageResponse]
    has_more: bool
    total: int


class MessageSearchResponse(BaseModel):
    messages: list[MessageResponse]
    total: int
    query: str
