"""
Whiteboard REST endpoints — session creation, stroke management, snapshots.

Architecture:
  - REST for persistent operations (CRUD on whiteboard sessions)
  - Socket.IO for real-time stroke broadcasting and cursor tracking
  - In-memory participant tracking cleared on disconnect
  - Snapshots enable efficient state transfer for late joiners
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.schemas.whiteboard import (
    SnapshotResponse,
    SnapshotSaveRequest,
    StrokeCreate,
    StrokeResponse,
    UserBrief,
    WhiteboardSessionCreate,
    WhiteboardSessionListItem,
    WhiteboardSessionResponse,
)
from app.services.channel_service import ChannelService
from app.services.whiteboard_service import WhiteboardService

logger = get_logger(__name__)

router = APIRouter(prefix="/whiteboards", tags=["whiteboards"])


# ─── Session Management ─────────────────────────────────────────────────


@router.post("", response_model=WhiteboardSessionResponse)
async def create_whiteboard(
    channel_id: str = Query(...),
    request: WhiteboardSessionCreate = ...,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new whiteboard session in a channel.

    User must be a channel member.

    Request:
      - name: Display name
      - width, height: Canvas dimensions (400-4096 px)
      - background_color: RGB hex (#rrggbb)
      - max_participants: Max concurrent editors (1-100)

    Response: WhiteboardSessionResponse with empty strokes array
    """
    try:
        # Verify user is channel member
        if not await ChannelService.is_member(db, channel_id, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a member of this channel",
            )

        session = await WhiteboardService.create_session(
            db=db,
            channel_id=channel_id,
            user_id=user_id,
            name=request.name,
            width=request.width,
            height=request.height,
            background_color=request.background_color,
            max_participants=request.max_participants,
        )

        return WhiteboardSessionResponse(
            id=session.id,
            channel_id=session.channel_id,
            name=session.name,
            created_by=session.created_by,
            is_active=session.is_active,
            max_participants=session.max_participants,
            background_color=session.background_color,
            width=session.width,
            height=session.height,
            strokes=[],
            created_at=session.created_at,
        )
    except Exception as e:
        logger.error("whiteboard_create_error", channel_id=channel_id, user_id=user_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get("/{session_id}", response_model=WhiteboardSessionResponse)
async def get_whiteboard(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch a whiteboard session with all strokes (for initial sync).

    Used by clients joining a whiteboard to reconstruct full canvas state.

    Returns: WhiteboardSessionResponse with all strokes in draw order
    """
    try:
        session = await WhiteboardService.get_session(db, session_id, include_strokes=True)

        # Convert strokes to response format
        stroke_responses = [
            StrokeResponse(
                id=stroke.id,
                tool=stroke.tool,
                color=stroke.color,
                width=stroke.width,
                opacity=stroke.opacity,
                points=__import__("json").loads(stroke.points),
                z_index=stroke.z_index,
                user=UserBrief(
                    id=stroke.user.id,
                    username=stroke.user.username,
                    display_name=stroke.user.display_name,
                    avatar_url=stroke.user.avatar_url,
                ),
                created_at=stroke.created_at,
            )
            for stroke in session.strokes
        ]

        return WhiteboardSessionResponse(
            id=session.id,
            channel_id=session.channel_id,
            name=session.name,
            created_by=session.created_by,
            is_active=session.is_active,
            max_participants=session.max_participants,
            background_color=session.background_color,
            width=session.width,
            height=session.height,
            strokes=stroke_responses,
            created_at=session.created_at,
        )
    except Exception as e:
        logger.error("whiteboard_get_error", session_id=session_id, user_id=user_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.get("/channel/{channel_id}", response_model=list[WhiteboardSessionListItem])
async def list_whiteboards(
    channel_id: str,
    active_only: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    List whiteboard sessions in a channel.

    Returns compact session info with stroke counts and participant counts.
    Useful for channel sidebar showing available canvases.

    Query params:
      - active_only: Filter to active sessions only
      - limit: Max results (1-200)

    Response: Array of WhiteboardSessionListItem
    """
    try:
        # Verify user is channel member
        if not await ChannelService.is_member(db, channel_id, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a member of this channel",
            )

        sessions_with_counts = await WhiteboardService.list_sessions(
            db=db,
            channel_id=channel_id,
            active_only=active_only,
            limit=limit,
        )

        return [
            WhiteboardSessionListItem(
                id=session.id,
                name=session.name,
                created_by=session.created_by,
                is_active=session.is_active,
                stroke_count=stroke_count,
                participant_count=participant_count,
                created_at=session.created_at,
            )
            for session, stroke_count, participant_count in sessions_with_counts
        ]
    except Exception as e:
        logger.error(
            "whiteboard_list_error", channel_id=channel_id, user_id=user_id, error=str(e)
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# ─── Snapshot Management ────────────────────────────────────────────────


@router.post("/{session_id}/snapshot", response_model=SnapshotResponse)
async def save_snapshot(
    session_id: str,
    request: SnapshotSaveRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Save a canvas state snapshot.

    Used for efficient state transfer when new participants join.

    Request:
      - snapshot_data: Serialized canvas (JSON strokes or base64 image)

    Response: SnapshotResponse with ID and timestamp

    Late-join workflow:
      1. New user joins whiteboard
      2. Fetch latest snapshot via REST (optional, can also fetch all strokes)
      3. Replay strokes after snapshot timestamp via socket
      4. Ready to draw
    """
    try:
        snapshot = await WhiteboardService.save_snapshot(
            db=db,
            session_id=session_id,
            user_id=user_id,
            snapshot_data=request.snapshot_data,
        )

        return SnapshotResponse(
            id=snapshot.id,
            session_id=snapshot.session_id,
            created_by=snapshot.created_by,
            snapshot_data=snapshot.snapshot_data,
            created_at=snapshot.created_at,
        )
    except Exception as e:
        logger.error(
            "whiteboard_snapshot_error",
            session_id=session_id,
            user_id=user_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# ─── Session Lifecycle ──────────────────────────────────────────────────


@router.delete("/{session_id}")
async def close_whiteboard(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Close (deactivate) a whiteboard session.

    Only the session creator can close it.
    Strokes and history are preserved; new participants cannot join.

    Response: {"status": "closed", "session_id": "..."}
    """
    try:
        session = await WhiteboardService.close_session(
            db=db,
            session_id=session_id,
            user_id=user_id,
        )

        return {
            "status": "closed",
            "session_id": session.id,
            "message": "Whiteboard closed. History preserved.",
        }
    except Exception as e:
        logger.error(
            "whiteboard_close_error",
            session_id=session_id,
            user_id=user_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
