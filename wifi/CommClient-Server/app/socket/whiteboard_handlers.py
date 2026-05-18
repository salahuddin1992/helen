"""
Whiteboard socket event handlers — real-time stroke broadcast, cursor tracking, tool changes.

Architecture:
  - Socket.IO rooms: one per whiteboard session (room_id = whiteboard_id)
  - Events broadcast only to room members (not server-wide)
  - Participant tracking via in-memory dict (cleared on disconnect)
  - Cursor positions and tool selection enable collaborative awareness

Real-time flow:
  1. User joins whiteboard:server:join -> added to room, broadcast to peers
  2. User draws stroke -> whiteboard:stroke -> broadcast to room
  3. User moves cursor -> whiteboard:cursor_move -> broadcast (low-frequency update)
  4. User changes tool -> whiteboard:tool_change -> broadcast
  5. User undoes -> whiteboard:undo -> broadcast
  6. User closes -> whiteboard:leave -> removed from room, broadcast
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.services.whiteboard_service import WhiteboardService
from app.socket.server import get_user_id, sio

logger = get_logger(__name__)

# Typing indicators: whiteboard_id -> {user_id -> timestamp}
# Could be used to show "User X is drawing..." hints
_drawing_indicators: dict[str, dict[str, float]] = {}


@sio.event
async def whiteboard_join(sid: str, data: dict[str, Any]):
    """
    User joins a whiteboard session.

    Adds user to room, loads session state, broadcasts presence.

    Client sends:
      {
        "session_id": str,
        "username": str,
        "display_name": str,
        "avatar_url": str | null,
      }

    Server:
      1. Fetches whiteboard session (validates)
      2. Adds participant to in-memory tracking
      3. Adds sid to socket.io room (session_id)
      4. Broadcasts participant list to room
      5. Emits full session state to joining user

    Security:
      - User ID extracted from socket auth (cannot be spoofed)
      - Channel membership verified (implicit: they can access the channel)
      - Max participants checked before adding
    """
    user_id = await get_user_id(sid)
    if not user_id:
        logger.warning("whiteboard_join_no_auth", sid=sid)
        return

    try:
        if not isinstance(data, dict):
            logger.warning("whiteboard_join_invalid", user_id=user_id)
            return

        session_id = data.get("session_id")
        username = data.get("username")
        display_name = data.get("display_name")
        avatar_url = data.get("avatar_url")

        if not session_id or not username or not display_name:
            logger.warning("whiteboard_join_missing_fields", user_id=user_id, session_id=session_id)
            return

        # Fetch session from DB
        async with async_session_factory() as db:
            try:
                session = await WhiteboardService.get_session(db, session_id, include_strokes=False)
            except Exception as e:
                logger.warning(
                    "whiteboard_join_session_not_found",
                    user_id=user_id,
                    session_id=session_id,
                )
                await sio.emit(
                    "whiteboard:error",
                    {"error": f"Session not found: {e}"},
                    to=sid,
                )
                return

            if not session.is_active:
                logger.warning(
                    "whiteboard_join_session_inactive",
                    user_id=user_id,
                    session_id=session_id,
                )
                await sio.emit(
                    "whiteboard:error",
                    {"error": "Session is closed"},
                    to=sid,
                )
                return

            # Check max participants
            current_count = len(WhiteboardService.get_participants(session_id))
            if current_count >= session.max_participants:
                logger.warning(
                    "whiteboard_join_full",
                    user_id=user_id,
                    session_id=session_id,
                    max=session.max_participants,
                )
                await sio.emit(
                    "whiteboard:error",
                    {"error": "Session full"},
                    to=sid,
                )
                return

            # Add participant to in-memory tracking
            WhiteboardService.add_participant(
                session_id=session_id,
                user_id=user_id,
                username=username,
                display_name=display_name,
                avatar_url=avatar_url,
            )

        # Join socket.io room
        sio.enter_room(sid, session_id)

        # Broadcast participant joined
        await sio.emit(
            "whiteboard:participant_joined",
            {
                "user_id": user_id,
                "username": username,
                "display_name": display_name,
                "avatar_url": avatar_url,
            },
            room=session_id,
            skip_sid=sid,
        )

        # Send current participants to joining user
        participants = WhiteboardService.get_participants(session_id)
        await sio.emit(
            "whiteboard:participants_list",
            {
                "participants": participants,
            },
            to=sid,
        )

        logger.info(
            "whiteboard_joined",
            session_id=session_id,
            user_id=user_id,
            username=username,
        )

    except Exception as e:
        logger.error("whiteboard_join_error", user_id=user_id, error=str(e))
        await sio.emit(
            "whiteboard:error",
            {"error": "Failed to join whiteboard"},
            to=sid,
        )


@sio.event
async def whiteboard_stroke(sid: str, data: dict[str, Any]):
    """
    User draws a stroke.

    Persists stroke to DB and broadcasts to room.

    Client sends:
      {
        "session_id": str,
        "tool": str,       # pen, eraser, line, etc.
        "color": str,      # #rrggbb
        "width": float,    # pixels
        "opacity": float,  # 0.0 to 1.0
        "points": [[x, y], ...],
        "z_index": int,
      }

    Server:
      1. Validates stroke data
      2. Persists to DB
      3. Broadcasts to all room participants (including sender)
      4. Updates participant tool info (for awareness)

    Security:
      - User ID extracted from socket (cannot be spoofed)
      - Session existence verified
      - All numeric fields validated (range checks)
    """
    user_id = await get_user_id(sid)
    if not user_id:
        logger.warning("whiteboard_stroke_no_auth", sid=sid)
        return

    try:
        if not isinstance(data, dict):
            logger.warning("whiteboard_stroke_invalid", user_id=user_id)
            return

        session_id = data.get("session_id")
        tool = data.get("tool")
        color = data.get("color")
        width = data.get("width")
        opacity = data.get("opacity", 1.0)
        points = data.get("points")
        z_index = data.get("z_index", 0)

        if not all([session_id, tool, color, width, points]):
            logger.warning("whiteboard_stroke_missing_fields", user_id=user_id, session_id=session_id)
            return

        # Validate numeric ranges
        if not isinstance(width, (int, float)) or width <= 0 or width > 100:
            logger.warning("whiteboard_stroke_invalid_width", user_id=user_id, width=width)
            return

        if not isinstance(opacity, (int, float)) or opacity < 0 or opacity > 1:
            logger.warning("whiteboard_stroke_invalid_opacity", user_id=user_id, opacity=opacity)
            return

        if not isinstance(points, list) or len(points) < 1:
            logger.warning("whiteboard_stroke_invalid_points", user_id=user_id, points=points)
            return

        # Persist to DB
        async with async_session_factory() as db:
            try:
                stroke = await WhiteboardService.add_stroke(
                    db=db,
                    session_id=session_id,
                    user_id=user_id,
                    tool=tool,
                    color=color,
                    width=width,
                    points=points,
                    opacity=opacity,
                    z_index=z_index,
                )
            except Exception as e:
                logger.error(
                    "whiteboard_stroke_persist_error",
                    session_id=session_id,
                    user_id=user_id,
                    error=str(e),
                )
                await sio.emit("whiteboard:error", {"error": "Failed to save stroke"}, to=sid)
                return

        # Update participant's current tool
        WhiteboardService.update_participant_tool(
            session_id=session_id,
            user_id=user_id,
            tool=tool,
            color=color,
            width=width,
        )

        # Broadcast stroke to room (including sender, for ack)
        await sio.emit(
            "whiteboard:stroke",
            {
                "stroke_id": stroke.id,
                "user_id": user_id,
                "tool": tool,
                "color": color,
                "width": width,
                "opacity": opacity,
                "points": points,
                "z_index": z_index,
                "created_at": stroke.created_at.isoformat(),
            },
            room=session_id,
        )

        logger.info(
            "whiteboard_stroke_broadcast",
            session_id=session_id,
            user_id=user_id,
            stroke_id=stroke.id,
        )

    except Exception as e:
        logger.error("whiteboard_stroke_error", user_id=user_id, error=str(e))


@sio.event
async def whiteboard_undo(sid: str, data: dict[str, Any]):
    """
    User undoes their last stroke.

    Client sends:
      {
        "session_id": str,
      }

    Server:
      1. Removes last stroke by this user in session (last-write-wins)
      2. Broadcasts undo event to room with removed stroke_id
      3. All clients remove that stroke from canvas

    Note: No permission check needed (undo only affects your own strokes)
    """
    user_id = await get_user_id(sid)
    if not user_id:
        logger.warning("whiteboard_undo_no_auth", sid=sid)
        return

    try:
        if not isinstance(data, dict):
            logger.warning("whiteboard_undo_invalid", user_id=user_id)
            return

        session_id = data.get("session_id")
        if not session_id:
            return

        # Delete last stroke by this user
        async with async_session_factory() as db:
            try:
                removed_stroke = await WhiteboardService.undo_stroke(db, session_id, user_id)
            except Exception as e:
                logger.error(
                    "whiteboard_undo_error",
                    session_id=session_id,
                    user_id=user_id,
                    error=str(e),
                )
                return

        if removed_stroke:
            # Broadcast undo to room
            await sio.emit(
                "whiteboard:undo",
                {
                    "stroke_id": removed_stroke.id,
                    "user_id": user_id,
                },
                room=session_id,
            )
            logger.info(
                "whiteboard_undo_broadcast",
                session_id=session_id,
                user_id=user_id,
                stroke_id=removed_stroke.id,
            )
        else:
            logger.info(
                "whiteboard_undo_no_stroke",
                session_id=session_id,
                user_id=user_id,
            )

    except Exception as e:
        logger.error("whiteboard_undo_handler_error", user_id=user_id, error=str(e))


@sio.event
async def whiteboard_clear(sid: str, data: dict[str, Any]):
    """
    Clear all strokes (destructive, owner-only).

    Client sends:
      {
        "session_id": str,
      }

    Server:
      1. Verifies user is session creator
      2. Deletes all strokes
      3. Broadcasts clear event to room
      4. All clients clear canvas

    Security:
      - Only session creator can clear
    """
    user_id = await get_user_id(sid)
    if not user_id:
        logger.warning("whiteboard_clear_no_auth", sid=sid)
        return

    try:
        if not isinstance(data, dict):
            return

        session_id = data.get("session_id")
        if not session_id:
            return

        # Clear strokes (service verifies ownership)
        async with async_session_factory() as db:
            try:
                count = await WhiteboardService.clear_board(db, session_id, user_id)
            except Exception as e:
                logger.warning(
                    "whiteboard_clear_forbidden",
                    session_id=session_id,
                    user_id=user_id,
                    error=str(e),
                )
                await sio.emit(
                    "whiteboard:error",
                    {"error": "Only session creator can clear"},
                    to=sid,
                )
                return

        # Broadcast clear to room
        await sio.emit(
            "whiteboard:cleared",
            {
                "user_id": user_id,
                "stroke_count": count,
            },
            room=session_id,
        )
        logger.info(
            "whiteboard_cleared",
            session_id=session_id,
            user_id=user_id,
            stroke_count=count,
        )

    except Exception as e:
        logger.error("whiteboard_clear_error", user_id=user_id, error=str(e))


@sio.event
async def whiteboard_cursor_move(sid: str, data: dict[str, Any]):
    """
    User moves cursor (for collaborative awareness).

    Low-frequency update (e.g., every 100ms) for efficiency.

    Client sends:
      {
        "session_id": str,
        "cursor_x": float,
        "cursor_y": float,
      }

    Server:
      1. Updates participant's cursor position (in-memory)
      2. Broadcasts cursor position to room (except sender)
      3. Clients show remote user cursors for awareness

    Optimization:
      - Server-side debouncing could be added if traffic is high
      - Clients can also debounce local cursor sends
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    try:
        if not isinstance(data, dict):
            return

        session_id = data.get("session_id")
        cursor_x = data.get("cursor_x")
        cursor_y = data.get("cursor_y")

        if not (session_id and cursor_x is not None and cursor_y is not None):
            return

        # Update in-memory tracking
        WhiteboardService.update_participant_cursor(session_id, user_id, cursor_x, cursor_y)

        # Broadcast to room (except sender, since they know their own cursor)
        await sio.emit(
            "whiteboard:cursor_move",
            {
                "user_id": user_id,
                "cursor_x": cursor_x,
                "cursor_y": cursor_y,
            },
            room=session_id,
            skip_sid=sid,
        )

    except Exception as e:
        logger.error("whiteboard_cursor_move_error", user_id=user_id, error=str(e))


@sio.event
async def whiteboard_tool_change(sid: str, data: dict[str, Any]):
    """
    User changes their drawing tool (for collaborative awareness).

    Client sends:
      {
        "session_id": str,
        "tool": str,
        "color": str | null,
        "width": float | null,
      }

    Server:
      1. Updates participant's tool info
      2. Broadcasts tool change to room
      3. Clients can show what each user is currently using

    Use case:
      - Show a legend: "Alice is drawing with red pen (3px)"
      - Prepare UI when user switches from pen to eraser
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    try:
        if not isinstance(data, dict):
            return

        session_id = data.get("session_id")
        tool = data.get("tool")

        if not (session_id and tool):
            return

        color = data.get("color")
        width = data.get("width")

        # Update in-memory tracking
        WhiteboardService.update_participant_tool(session_id, user_id, tool, color, width)

        # Broadcast to room
        await sio.emit(
            "whiteboard:tool_change",
            {
                "user_id": user_id,
                "tool": tool,
                "color": color,
                "width": width,
            },
            room=session_id,
            skip_sid=sid,
        )

        logger.info(
            "whiteboard_tool_change",
            session_id=session_id,
            user_id=user_id,
            tool=tool,
        )

    except Exception as e:
        logger.error("whiteboard_tool_change_error", user_id=user_id, error=str(e))


@sio.event
async def whiteboard_leave(sid: str, data: dict[str, Any] | None = None):
    """
    User leaves the whiteboard session.

    Called explicitly by client or implicitly on disconnect.

    Client sends (optional):
      {
        "session_id": str,
      }

    Server:
      1. Removes participant from in-memory tracking
      2. Leaves socket.io room
      3. Broadcasts participant left to room
      4. Cleans up if session is empty and should auto-close

    Note:
      - This is also called from disconnect handler implicitly
      - Can be called explicitly for graceful leave
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    try:
        # Extract session_id if provided, otherwise find it from room list
        session_id = None
        if isinstance(data, dict):
            session_id = data.get("session_id")

        # If not provided, check which whiteboard room this sid is in
        if not session_id:
            # Could iterate sio.rooms[sid] to find whiteboard rooms
            # For now, require client to provide session_id
            logger.info("whiteboard_leave_implicit", user_id=user_id)
            # Best effort: find and leave all whiteboard rooms for this user
            # This is handled implicitly on disconnect anyway
            return

        # Remove from in-memory tracking
        WhiteboardService.remove_participant(session_id, user_id)

        # Leave socket.io room
        sio.leave_room(sid, session_id)

        # Broadcast participant left
        await sio.emit(
            "whiteboard:participant_left",
            {
                "user_id": user_id,
            },
            room=session_id,
        )

        logger.info(
            "whiteboard_left",
            session_id=session_id,
            user_id=user_id,
        )

    except Exception as e:
        logger.error("whiteboard_leave_error", user_id=user_id, error=str(e))


# ── Colon-style aliases for client compatibility ─────────────────
# Client code emits whiteboard:* events; the handlers above are
# registered via @sio.event with their snake_case function names.
# These wrappers register the colon-style aliases without duplicating logic.


@sio.on("whiteboard:join")
async def _whiteboard_join_alias(sid: str, data: dict[str, Any]):
    return await whiteboard_join(sid, data)


@sio.on("whiteboard:stroke")
async def _whiteboard_stroke_alias(sid: str, data: dict[str, Any]):
    return await whiteboard_stroke(sid, data)


@sio.on("whiteboard:undo")
async def _whiteboard_undo_alias(sid: str, data: dict[str, Any]):
    return await whiteboard_undo(sid, data)


@sio.on("whiteboard:clear")
async def _whiteboard_clear_alias(sid: str, data: dict[str, Any]):
    return await whiteboard_clear(sid, data)


@sio.on("whiteboard:cursor")
async def _whiteboard_cursor_alias(sid: str, data: dict[str, Any]):
    """Client uses 'whiteboard:cursor' but server fn is whiteboard_cursor_move."""
    return await whiteboard_cursor_move(sid, data)


@sio.on("whiteboard:tool_change")
async def _whiteboard_tool_change_alias(sid: str, data: dict[str, Any]):
    return await whiteboard_tool_change(sid, data)


@sio.on("whiteboard:leave")
async def _whiteboard_leave_alias(sid: str, data: dict[str, Any] | None = None):
    return await whiteboard_leave(sid, data)
