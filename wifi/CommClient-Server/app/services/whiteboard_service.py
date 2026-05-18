"""
Whiteboard service — collaborative canvas state management.

Architecture:
  - Stroke-based (not bitmap): each drawing action is persisted as a stroke
  - Snapshot caching: efficient state transfer for late joiners
  - In-memory participant tracking: cleared on disconnect
  - Undo/redo via last-write-wins (last stroke by user, per session)
  - No conflict resolution needed: concurrent strokes are independent

Concurrency model:
  - Strokes are append-only and immutable
  - Undo removes last stroke by same user (idempotent)
  - Clear board requires ownership (creator or channel owner)
  - Snapshots are point-in-time views (read-only)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.channel import ChannelMember
from app.models.user import User
from app.models.whiteboard import (
    WhiteboardSession,
    WhiteboardSnapshot,
    WhiteboardStroke,
)

logger = get_logger(__name__)

# In-memory participant tracking: whiteboard_id -> {user_id -> participant_info}
_whiteboard_participants: dict[str, dict[str, dict[str, Any]]] = {}


class WhiteboardService:
    """Singleton service for whiteboard management."""

    @staticmethod
    async def create_session(
        db: AsyncSession,
        channel_id: str,
        user_id: str,
        name: str,
        width: int = 1920,
        height: int = 1080,
        background_color: str = "#ffffff",
        max_participants: int = 10,
    ) -> WhiteboardSession:
        """
        Create a new whiteboard session.

        User must be a channel member to create a whiteboard.

        Returns: WhiteboardSession object
        """
        # Verify user is member of channel
        result = await db.execute(
            select(ChannelMember).where(
                and_(
                    ChannelMember.channel_id == channel_id,
                    ChannelMember.user_id == user_id,
                )
            )
        )
        if not result.scalar_one_or_none():
            raise ForbiddenError("You are not a member of this channel")

        session = WhiteboardSession(
            channel_id=channel_id,
            name=name,
            created_by=user_id,
            width=width,
            height=height,
            background_color=background_color,
            max_participants=max_participants,
            is_active=True,
        )
        db.add(session)
        await db.commit()

        # Initialize empty participant tracking
        _whiteboard_participants[session.id] = {}

        logger.info(
            "whiteboard_session_created",
            session_id=session.id,
            channel_id=channel_id,
            creator=user_id,
        )
        return session

    @staticmethod
    async def get_session(
        db: AsyncSession,
        session_id: str,
        include_strokes: bool = True,
    ) -> WhiteboardSession:
        """
        Fetch a whiteboard session by ID.

        If include_strokes=True, load all strokes (for initial sync).
        Otherwise, return session metadata only.

        Returns: WhiteboardSession with strokes if requested
        Raises: NotFoundError if session doesn't exist
        """
        query = select(WhiteboardSession).where(WhiteboardSession.id == session_id)

        if include_strokes:
            query = query.options(
                selectinload(WhiteboardSession.strokes).selectinload(WhiteboardStroke.user)
            )

        result = await db.execute(query)
        session = result.scalar_one_or_none()

        if not session:
            raise NotFoundError(f"Whiteboard session {session_id} not found")

        return session

    @staticmethod
    async def add_stroke(
        db: AsyncSession,
        session_id: str,
        user_id: str,
        tool: str,
        color: str,
        width: float,
        points: list[list[float]],
        opacity: float = 1.0,
        z_index: int = 0,
    ) -> WhiteboardStroke:
        """
        Add a stroke to the canvas.

        Persists the stroke and returns it with user details.

        Args:
          session_id: Whiteboard session ID
          user_id: Who's drawing
          tool: pen, eraser, line, rectangle, circle, etc.
          color: RGB hex (e.g., "#ff0000")
          width: Brush width in pixels
          points: [[x, y], [x, y], ...] drawing path
          opacity: 0.0 to 1.0
          z_index: Layer order

        Returns: WhiteboardStroke object
        Raises: NotFoundError, ValidationError
        """
        # Verify session exists and is active
        result = await db.execute(
            select(WhiteboardSession).where(
                and_(
                    WhiteboardSession.id == session_id,
                    WhiteboardSession.is_active == True,
                )
            )
        )
        session = result.scalar_one_or_none()
        if not session:
            raise NotFoundError("Whiteboard session not found or inactive")

        # Validate points
        if not points or len(points) < 1:
            raise ValidationError("Stroke must have at least one point")

        # Serialize points as JSON
        points_json = json.dumps(points)

        stroke = WhiteboardStroke(
            session_id=session_id,
            user_id=user_id,
            tool=tool,
            color=color,
            width=width,
            opacity=opacity,
            points=points_json,
            z_index=z_index,
        )
        db.add(stroke)
        await db.commit()

        # Reload with user details
        result = await db.execute(
            select(WhiteboardStroke)
            .where(WhiteboardStroke.id == stroke.id)
            .options(selectinload(WhiteboardStroke.user))
        )
        stroke = result.scalar_one()

        logger.info(
            "whiteboard_stroke_added",
            session_id=session_id,
            user_id=user_id,
            stroke_id=stroke.id,
        )
        return stroke

    @staticmethod
    async def undo_stroke(db: AsyncSession, session_id: str, user_id: str) -> WhiteboardStroke | None:
        """
        Remove the last stroke drawn by this user in the session (last-write-wins undo).

        Returns: The removed stroke, or None if no stroke to undo
        Raises: NotFoundError
        """
        # Find latest stroke by this user
        result = await db.execute(
            select(WhiteboardStroke)
            .where(
                and_(
                    WhiteboardStroke.session_id == session_id,
                    WhiteboardStroke.user_id == user_id,
                )
            )
            .order_by(desc(WhiteboardStroke.created_at))
            .limit(1)
        )
        stroke = result.scalar_one_or_none()

        if not stroke:
            logger.info(
                "whiteboard_undo_no_stroke",
                session_id=session_id,
                user_id=user_id,
            )
            return None

        # Delete it
        await db.delete(stroke)
        await db.commit()

        logger.info(
            "whiteboard_stroke_undone",
            session_id=session_id,
            user_id=user_id,
            stroke_id=stroke.id,
        )
        return stroke

    @staticmethod
    async def clear_board(
        db: AsyncSession,
        session_id: str,
        user_id: str,
    ) -> int:
        """
        Clear all strokes from a whiteboard (destructive).

        Only the session creator or channel owner can do this.
        Requires membership/ownership verification in route.

        Returns: Count of strokes deleted
        """
        # Verify user is owner/creator
        result = await db.execute(
            select(WhiteboardSession).where(WhiteboardSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if not session:
            raise NotFoundError("Whiteboard session not found")

        if session.created_by != user_id:
            raise ForbiddenError("Only the session creator can clear the board")

        # Delete all strokes
        result = await db.execute(
            select(func.count())
            .select_from(WhiteboardStroke)
            .where(WhiteboardStroke.session_id == session_id)
        )
        count = result.scalar() or 0

        await db.execute(
            select(WhiteboardStroke)
            .where(WhiteboardStroke.session_id == session_id)
        )
        for stroke in (await db.execute(
            select(WhiteboardStroke).where(WhiteboardStroke.session_id == session_id)
        )).scalars():
            await db.delete(stroke)

        await db.commit()

        logger.info(
            "whiteboard_board_cleared",
            session_id=session_id,
            user_id=user_id,
            stroke_count=count,
        )
        return count

    @staticmethod
    async def save_snapshot(
        db: AsyncSession,
        session_id: str,
        user_id: str,
        snapshot_data: str,
    ) -> WhiteboardSnapshot:
        """
        Save a canvas snapshot (for efficient state transfer).

        Snapshot data is typically JSON-serialized stroke array or base64 image.

        Returns: WhiteboardSnapshot object
        """
        snapshot = WhiteboardSnapshot(
            session_id=session_id,
            created_by=user_id,
            snapshot_data=snapshot_data,
        )
        db.add(snapshot)
        await db.commit()

        logger.info(
            "whiteboard_snapshot_saved",
            session_id=session_id,
            snapshot_id=snapshot.id,
        )
        return snapshot

    @staticmethod
    async def list_sessions(
        db: AsyncSession,
        channel_id: str,
        active_only: bool = True,
        limit: int = 50,
    ) -> list[tuple[WhiteboardSession, int, int]]:
        """
        List whiteboard sessions for a channel.

        Returns list of (session, stroke_count, participant_count) tuples.
        """
        query = select(WhiteboardSession).where(
            WhiteboardSession.channel_id == channel_id
        )

        if active_only:
            query = query.where(WhiteboardSession.is_active == True)

        query = query.order_by(desc(WhiteboardSession.created_at)).limit(limit)

        result = await db.execute(query)
        sessions = result.scalars().all()

        sessions_with_counts = []
        for session in sessions:
            # Count strokes
            stroke_result = await db.execute(
                select(func.count())
                .select_from(WhiteboardStroke)
                .where(WhiteboardStroke.session_id == session.id)
            )
            stroke_count = stroke_result.scalar() or 0

            # Get participant count from in-memory tracking
            participant_count = len(_whiteboard_participants.get(session.id, {}))

            sessions_with_counts.append((session, stroke_count, participant_count))

        return sessions_with_counts

    @staticmethod
    async def close_session(db: AsyncSession, session_id: str, user_id: str) -> WhiteboardSession:
        """
        Close (deactivate) a whiteboard session.

        Only creator can close. Prevents new participants from joining
        but doesn't delete strokes or history.

        Returns: Closed session
        """
        result = await db.execute(
            select(WhiteboardSession).where(WhiteboardSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if not session:
            raise NotFoundError("Whiteboard session not found")

        if session.created_by != user_id:
            raise ForbiddenError("Only the session creator can close it")

        session.is_active = False
        await db.commit()

        # Cleanup in-memory participants
        if session_id in _whiteboard_participants:
            del _whiteboard_participants[session_id]

        logger.info(
            "whiteboard_session_closed",
            session_id=session_id,
            user_id=user_id,
        )
        return session

    # ─── In-Memory Participant Tracking ──────────────────────────────────────

    @staticmethod
    def add_participant(
        session_id: str,
        user_id: str,
        username: str,
        display_name: str,
        avatar_url: str | None = None,
    ) -> dict[str, Any]:
        """Track a participant joining the whiteboard (in-memory)."""
        if session_id not in _whiteboard_participants:
            _whiteboard_participants[session_id] = {}

        participant_info = {
            "user_id": user_id,
            "username": username,
            "display_name": display_name,
            "avatar_url": avatar_url,
            "cursor_x": None,
            "cursor_y": None,
            "current_tool": None,
            "current_color": None,
        }
        _whiteboard_participants[session_id][user_id] = participant_info

        logger.info(
            "whiteboard_participant_joined",
            session_id=session_id,
            user_id=user_id,
            total_participants=len(_whiteboard_participants[session_id]),
        )
        return participant_info

    @staticmethod
    def remove_participant(session_id: str, user_id: str) -> None:
        """Track participant leaving."""
        if session_id in _whiteboard_participants:
            _whiteboard_participants[session_id].pop(user_id, None)
            logger.info(
                "whiteboard_participant_left",
                session_id=session_id,
                user_id=user_id,
                remaining=len(_whiteboard_participants[session_id]),
            )

    @staticmethod
    def update_participant_cursor(
        session_id: str,
        user_id: str,
        cursor_x: float,
        cursor_y: float,
    ) -> None:
        """Update participant's cursor position."""
        if session_id in _whiteboard_participants:
            if user_id in _whiteboard_participants[session_id]:
                _whiteboard_participants[session_id][user_id]["cursor_x"] = cursor_x
                _whiteboard_participants[session_id][user_id]["cursor_y"] = cursor_y

    @staticmethod
    def update_participant_tool(
        session_id: str,
        user_id: str,
        tool: str,
        color: str | None = None,
        width: float | None = None,
    ) -> None:
        """Update participant's active tool and drawing settings."""
        if session_id in _whiteboard_participants:
            if user_id in _whiteboard_participants[session_id]:
                _whiteboard_participants[session_id][user_id]["current_tool"] = tool
                if color:
                    _whiteboard_participants[session_id][user_id]["current_color"] = color
                if width is not None:
                    _whiteboard_participants[session_id][user_id]["current_width"] = width

    @staticmethod
    def get_participants(session_id: str) -> list[dict[str, Any]]:
        """Get list of all participants in a session."""
        return list(_whiteboard_participants.get(session_id, {}).values())

    @staticmethod
    def cleanup_session_participants(session_id: str) -> None:
        """Clear participant tracking when session closes."""
        _whiteboard_participants.pop(session_id, None)
        logger.info("whiteboard_participants_cleanup", session_id=session_id)
