"""
Session / device management schemas.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SessionResponse(BaseModel):
    id: str
    device_name: str | None
    ip_address: str | None
    is_active: bool
    last_activity: datetime
    created_at: datetime

    class Config:
        from_attributes = True


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int
