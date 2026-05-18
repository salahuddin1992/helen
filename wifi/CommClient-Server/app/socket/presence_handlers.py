"""
Presence socket event handlers — heartbeat, status updates, custom status messages.
"""

from __future__ import annotations

from datetime import datetime

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.services.presence_service import presence_service
from app.services.user_service import UserService
from app.socket.server import get_user_id, sio

logger = get_logger(__name__)


@sio.event
async def presence_heartbeat(sid: str, data: dict | None = None):
    """Client sends periodic heartbeat to keep alive."""
    user_id = await get_user_id(sid)
    if user_id:
        await presence_service.heartbeat(user_id)


@sio.event
async def presence_set_status(sid: str, data: dict):
    """
    Client sets their status.
    data: { status: "online" | "away" | "busy" | "dnd" }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    status = data.get("status", "online")
    if status not in ("online", "away", "busy", "dnd"):
        return

    await presence_service.set_status(user_id, status)
    # Local broadcast first (skip_sid so the originator doesn't get
    # their own change echoed). Then cross-server broadcast via
    # fabric_emit (allowlist gates the fabric path; no-op when not
    # opted in — every other server still sees the local sio.emit
    # because socketio Redis adapter handles room-less broadcasts
    # when configured).
    await sio.emit("presence:user_status", {
        "user_id": user_id,
        "status": status,
    }, skip_sid=sid)

    from app.services import fabric_emit as _fe
    await _fe.emit_broadcast(
        event_type="presence:user_status",
        priority="P3",
        payload={"user_id": user_id, "status": status},
        source_user_id=user_id,
    )

    logger.info("presence_status_changed", user_id=user_id, status=status)


@sio.event
async def presence_set_status_message(sid: str, data: dict):
    """
    Client sets their custom status message.
    data: {
        status_message: str | null,           # null = clear
        status_expires_at?: ISO 8601 string,  # optional auto-clear time
    }
    Returns: { ok: True, status_message, status_expires_at } or { error }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    raw_msg = data.get("status_message")
    if raw_msg is not None:
        if not isinstance(raw_msg, str):
            return {"error": "status_message must be a string or null"}
        raw_msg = raw_msg.strip()
        if len(raw_msg) > 140:
            return {"error": "status_message exceeds 140 characters"}
        if not raw_msg:
            raw_msg = None  # empty string == clear

    expires_at = None
    raw_expires = data.get("status_expires_at")
    if raw_expires:
        try:
            if isinstance(raw_expires, str):
                expires_at = datetime.fromisoformat(raw_expires.replace("Z", "+00:00"))
            elif isinstance(raw_expires, (int, float)):
                # Treat as epoch seconds
                from datetime import timezone as _tz
                expires_at = datetime.fromtimestamp(float(raw_expires), tz=_tz.utc)
        except (ValueError, TypeError):
            return {"error": "status_expires_at must be ISO 8601"}

    try:
        async with async_session_factory() as db:
            user = await UserService.set_status_message(
                db, user_id, status_message=raw_msg, status_expires_at=expires_at
            )

        payload = {
            "user_id": user_id,
            "status_message": user.status_message,
            "status_expires_at": user.status_expires_at.isoformat() if user.status_expires_at else None,
        }

        # Broadcast to everyone except the originating socket
        await sio.emit("presence:status_message_changed", payload, skip_sid=sid)

        logger.info(
            "presence_status_message_set",
            user_id=user_id,
            cleared=raw_msg is None,
        )
        return {"ok": True, **payload}

    except Exception as e:
        logger.error("presence_set_status_message_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


# Colon-style alias used by some clients
@sio.on("presence:set_status_message")
async def _presence_set_status_message_alias(sid: str, data: dict):
    return await presence_set_status_message(sid, data)
