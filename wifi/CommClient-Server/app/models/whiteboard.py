"""
Collaborative whiteboard models — real-time drawing canvas with stroke history.

Architecture:
  - WhiteboardSession: Container for collaborative drawing (channel-scoped)
  - WhiteboardStroke: Individual brush strokes (persisted for undo/replay)
  - WhiteboardSnapshot: Canvas state snapshots (for efficient state sync)

Concurrency model:
  - Strokes are appended only (immutable history)
  - Undo removes last stroke by same user (last-write-wins)
  - Snapshots compress history for efficient catchup
  - In-memory participant tracking (cleared on disconnect)
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WhiteboardSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A shared whiteboard canvas within a channel.
    One or more whiteboards can exist per channel.

    Lifecycle:
      1. Created by any channel member
      2. Participants join/leave (tracked in-memory)
      3. Strokes are drawn and persisted
      4. Optional: save snapshots for efficient state transfer
      5. Closed by owner or last participant leaves
    """

    __tablename__ = "whiteboard_sessions"

    channel_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_by: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    max_participants: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

    # Canvas dimensions and styling
    background_color: Mapped[str] = mapped_column(String(32), default="#ffffff", nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=1920, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=1080, nullable=False)

    # Relationships
    channel: Mapped["Channel"] = relationship("Channel")
    creator: Mapped["User"] = relationship("User")
    strokes: Mapped[list["WhiteboardStroke"]] = relationship(
        "WhiteboardStroke",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    snapshots: Mapped[list["WhiteboardSnapshot"]] = relationship(
        "WhiteboardSnapshot",
        back_populates="session",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<WhiteboardSession {self.id[:8]} in {self.channel_id[:8]} by {self.created_by[:4]}>"


class WhiteboardStroke(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A single brush stroke on the canvas.

    Immutable record of each drawing action.
    Enables:
      - Undo/redo (by removing/replaying strokes)
      - Replay for late joiners (re-render from start)
      - Audit trail (who drew what and when)

    Stroke format:
      - tool: "pen", "eraser", "line", "rectangle", "circle", etc.
      - color: RGB hex string (e.g., "#ff0000")
      - width: Brush width in pixels (float)
      - opacity: 0.0 to 1.0
      - points: JSON array of [x, y] coordinates or [[x,y],[x,y],...]
      - z_index: Layer ordering (higher = on top)
    """

    __tablename__ = "whiteboard_strokes"

    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("whiteboard_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool: Mapped[str] = mapped_column(String(32), nullable=False)  # pen, eraser, line, etc.
    color: Mapped[str] = mapped_column(String(32), nullable=False)  # #rrggbb or rgba
    width: Mapped[float] = mapped_column(Float, nullable=False)  # Brush size in pixels
    opacity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)  # 0.0 to 1.0
    points: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    z_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # Layer order

    # Relationships
    session: Mapped["WhiteboardSession"] = relationship("WhiteboardSession", back_populates="strokes")
    user: Mapped["User"] = relationship("User")

    def __repr__(self) -> str:
        return f"<WhiteboardStroke {self.id[:8]} {self.tool} by {self.user_id[:4]}>"


class WhiteboardSnapshot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Compressed canvas state snapshot.

    Used for efficient state synchronization:
      1. Client joins whiteboard
      2. Fetch latest snapshot (compressed full canvas state)
      3. Replay strokes after snapshot timestamp
      4. Much faster than replaying all strokes from session start

    Snapshot data format:
      - Can be JSON representation of canvas (easiest for client)
      - Or binary PNG/WebP for true compression (requires server-side render)
      - Typically stored as base64 or JSON array of stroke objects

    Re-creation workflow:
      1. Any participant can trigger snapshot save
      2. Server captures current canvas state (aggregated from strokes)
      3. Snapshot is persisted for future joiners
      4. Snapshots older than N most-recent are candidates for cleanup
    """

    __tablename__ = "whiteboard_snapshots"

    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("whiteboard_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    snapshot_data: Mapped[str] = mapped_column(Text, nullable=False)  # JSON or base64

    # Relationships
    session: Mapped["WhiteboardSession"] = relationship("WhiteboardSession", back_populates="snapshots")
    creator: Mapped["User | None"] = relationship("User")

    def __repr__(self) -> str:
        return f"<WhiteboardSnapshot {self.id[:8]} for {self.session_id[:8]}>"
