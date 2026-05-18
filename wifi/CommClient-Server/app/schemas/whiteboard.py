"""
Whiteboard schemas (Pydantic v2).

Optimized for real-time socket.io transmission and efficient client rendering.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ─── Create/Update Schemas ───────────────────────────────────────────


class WhiteboardSessionCreate(BaseModel):
    """Request to create a new whiteboard session."""

    name: str = Field(..., min_length=1, max_length=256, description="Display name for whiteboard")
    width: int = Field(default=1920, ge=400, le=4096, description="Canvas width in pixels")
    height: int = Field(default=1080, ge=400, le=4096, description="Canvas height in pixels")
    background_color: str = Field(
        default="#ffffff",
        pattern="^#[0-9a-fA-F]{6}$",
        description="RGB hex color",
    )
    max_participants: int = Field(default=10, ge=1, le=100, description="Max simultaneous editors")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Team Brainstorm - Q1 Planning",
                "width": 1920,
                "height": 1080,
                "background_color": "#ffffff",
                "max_participants": 10,
            }
        }


class StrokeData(BaseModel):
    """A single brush stroke."""

    tool: str = Field(..., description="pen, eraser, line, rectangle, circle, text, etc.")
    color: str = Field(..., pattern="^#[0-9a-fA-F]{6}$", description="RGB hex color")
    width: float = Field(..., gt=0, le=100, description="Brush width in pixels")
    opacity: float = Field(default=1.0, ge=0.0, le=1.0, description="Transparency 0-1")
    points: list[list[float]] = Field(..., description="[[x,y], [x,y], ...] drawing path")
    z_index: int = Field(default=0, description="Layer ordering")

    class Config:
        json_schema_extra = {
            "example": {
                "tool": "pen",
                "color": "#ff0000",
                "width": 3.5,
                "opacity": 1.0,
                "points": [[10, 20], [15, 25], [20, 30]],
                "z_index": 0,
            }
        }


class StrokeCreate(StrokeData):
    """Request to add a stroke to the canvas."""

    pass


# ─── Response Schemas ───────────────────────────────────────────────


class UserBrief(BaseModel):
    """Minimal user info for broadcast messages."""

    id: str = Field(..., description="User ID")
    username: str = Field(..., description="Username")
    display_name: str = Field(..., description="Display name")
    avatar_url: str | None = Field(None, description="Avatar image URL")


class StrokeResponse(StrokeData):
    """Stroke as returned from server."""

    id: str = Field(..., description="Stroke ID")
    user: UserBrief = Field(..., description="Who drew this stroke")
    created_at: datetime = Field(..., description="Timestamp")

    class Config:
        json_schema_extra = {
            "example": {
                "id": "stroke123abc",
                "tool": "pen",
                "color": "#ff0000",
                "width": 3.5,
                "opacity": 1.0,
                "points": [[10, 20], [15, 25], [20, 30]],
                "z_index": 0,
                "user": {
                    "id": "user1",
                    "username": "alice",
                    "display_name": "Alice",
                    "avatar_url": "https://...",
                },
                "created_at": "2026-04-09T10:30:00Z",
            }
        }


class WhiteboardSessionResponse(BaseModel):
    """Full whiteboard session with current strokes."""

    id: str = Field(..., description="Session ID")
    channel_id: str = Field(..., description="Parent channel ID")
    name: str = Field(..., description="Display name")
    created_by: str = Field(..., description="Creator user ID")
    is_active: bool = Field(..., description="Is session accepting new participants")
    max_participants: int = Field(..., description="Max participant cap")
    background_color: str = Field(..., description="Canvas background RGB hex")
    width: int = Field(..., description="Canvas width")
    height: int = Field(..., description="Canvas height")
    strokes: list[StrokeResponse] = Field(default_factory=list, description="All strokes in order")
    created_at: datetime = Field(..., description="Session creation timestamp")

    class Config:
        json_schema_extra = {
            "example": {
                "id": "wb123",
                "channel_id": "ch456",
                "name": "Brainstorm",
                "created_by": "user1",
                "is_active": True,
                "max_participants": 10,
                "background_color": "#ffffff",
                "width": 1920,
                "height": 1080,
                "strokes": [],
                "created_at": "2026-04-09T10:00:00Z",
            }
        }


class WhiteboardSessionListItem(BaseModel):
    """Compact whiteboard session for listing."""

    id: str = Field(..., description="Session ID")
    name: str = Field(..., description="Display name")
    created_by: str = Field(..., description="Creator user ID")
    is_active: bool = Field(..., description="Is active")
    stroke_count: int = Field(..., description="Total strokes")
    participant_count: int = Field(..., description="Currently connected participants")
    created_at: datetime = Field(..., description="Creation timestamp")

    class Config:
        json_schema_extra = {
            "example": {
                "id": "wb123",
                "name": "Q1 Planning",
                "created_by": "user1",
                "is_active": True,
                "stroke_count": 42,
                "participant_count": 3,
                "created_at": "2026-04-09T10:00:00Z",
            }
        }


class SnapshotSaveRequest(BaseModel):
    """Request to save a canvas snapshot."""

    snapshot_data: str = Field(..., description="Serialized canvas state (JSON or base64)")


class SnapshotResponse(BaseModel):
    """Canvas snapshot metadata and data."""

    id: str = Field(..., description="Snapshot ID")
    session_id: str = Field(..., description="Parent session ID")
    created_by: str | None = Field(None, description="Who saved this snapshot")
    snapshot_data: str = Field(..., description="Canvas state data")
    created_at: datetime = Field(..., description="Timestamp")


# ─── Socket Event Schemas ───────────────────────────────────────────


class StrokeEvent(BaseModel):
    """Stroke broadcast via socket (real-time)."""

    stroke_id: str = Field(..., description="Stroke ID")
    user_id: str = Field(..., description="User who drew")
    tool: str
    color: str
    width: float
    opacity: float
    points: list[list[float]]
    z_index: int
    created_at: datetime = Field(..., description="Server timestamp")


class ParticipantInfo(BaseModel):
    """Active participant in whiteboard."""

    user_id: str = Field(..., description="User ID")
    username: str = Field(..., description="Username")
    display_name: str = Field(..., description="Display name")
    avatar_url: str | None = Field(None)
    cursor_x: float | None = Field(None, description="Current cursor X")
    cursor_y: float | None = Field(None, description="Current cursor Y")
    current_tool: str | None = Field(None, description="Tool they're using")
    current_color: str | None = Field(None, description="Color they're using")


class CursorUpdateEvent(BaseModel):
    """Cursor position broadcast for collaborative awareness."""

    user_id: str
    cursor_x: float
    cursor_y: float


class ToolChangeEvent(BaseModel):
    """Tool selection broadcast for awareness of peer activity."""

    user_id: str
    tool: str
    color: str | None = Field(None)
    width: float | None = Field(None)
