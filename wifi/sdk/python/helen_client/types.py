"""Typed dataclasses mirroring Helen-Server's API responses.

We intentionally hand-write these instead of generating from
OpenAPI — the API is stable and hand-rolled types are friendlier
to debug.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AuthToken:
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "Bearer"
    expires_in: int = 0


@dataclass
class User:
    id: str
    username: str
    display_name: str
    avatar_url: Optional[str] = None
    status: str = "offline"            # online | away | busy | dnd | offline
    role: str = "member"


@dataclass
class Channel:
    id: str
    name: str
    type: str = "channel"              # channel | dm | group
    members_count: int = 0
    last_message_at: float = 0.0


@dataclass
class Message:
    id: str
    channel_id: str
    sender_id: str
    content: str
    sent_at: float
    edited_at: Optional[float] = None
    reply_to: Optional[str] = None
    attachments: list[dict] = field(default_factory=list)
    reactions: dict[str, list[str]] = field(default_factory=dict)
    encrypted: bool = False


@dataclass
class Call:
    id: str
    channel_id: str
    started_by: str
    started_at: float
    ended_at: Optional[float] = None
    participants: list[str] = field(default_factory=list)
    is_video: bool = False


@dataclass
class KeyBundle:
    """Snapshot of a user's E2EE key bundle (public-only)."""
    user_id: str
    identity_pub: str            # hex
    signing_pub: str
    signed_pre_pub: str
    signed_pre_id: int
    signed_pre_sig: str
    one_time_pre_pubs: list[tuple[int, str]] = field(default_factory=list)
