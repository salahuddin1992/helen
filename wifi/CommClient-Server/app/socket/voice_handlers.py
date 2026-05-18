"""
Voice message socket event handlers — real-time notifications, presence indicators.

Events:
- voice_message_sent — Notify channel when voice message is sent
- voice_message_playing — Update presence when user starts playing message
- voice_message_stopped — Update presence when user stops playing message
- voice_message_deleted — Notify channel when message is deleted
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.audit import audit_permission_denied
from app.core.logging import get_logger
from app.core.security_utils import (
    cache_membership,
    get_cached_membership,
    is_valid_uuid,
)
from app.db.session import async_session_factory
from app.services.channel_service import ChannelService
from app.socket.server import get_user_id, sio

logger = get_logger(__name__)


async def _verify_channel_membership(
    channel_id: str, user_id: str, action: str = "unknown"
) -> bool:
    """
    Verify user is a member of a channel.

    Returns True if member, False if not. Logs unauthorized attempts.
    """
    # Input validation
    if not is_valid_uuid(channel_id) or not is_valid_uuid(user_id):
        return False

    # Check cache first
    cached = get_cached_membership(channel_id, user_id)
    if cached is not None:
        if not cached:
            audit_permission_denied(user_id, f"channel:{channel_id}", action)
        return cached

    # DB lookup
    try:
        async with async_session_factory() as db:
            is_member = await ChannelService.is_member(db, channel_id, user_id)
            cache_membership(channel_id, user_id, is_member)
            if not is_member:
                audit_permission_denied(user_id, f"channel:{channel_id}", action)
                logger.warning(
                    "channel_access_denied",
                    user_id=user_id,
                    channel_id=channel_id,
                    action=action,
                )
            return is_member
    except Exception as e:
        logger.error(
            "membership_check_error",
            error=str(e),
            channel_id=channel_id,
            user_id=user_id,
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Voice Message Sent
# ─────────────────────────────────────────────────────────────────────────────


@sio.event
async def voice_message_sent(sid: str, data: dict):
    """
    Notify channel when voice message is sent.

    data: {
        channel_id: str,
        voice_message_id: str,
        duration_ms: int,
        mime_type: str,
        waveform_data: list[float],
        created_at: str (ISO),
    }

    Broadcasting to channel immediately notifies all members.
    """
    try:
        user_id = await get_user_id(sid)
        if not user_id:
            logger.warning("voice_message_sent_no_user", sid=sid)
            return

        channel_id = data.get("channel_id")
        voice_message_id = data.get("voice_message_id")

        if not channel_id or not voice_message_id:
            logger.warning(
                "voice_message_sent_invalid_data",
                sid=sid,
                user_id=user_id,
            )
            return

        # Verify channel membership
        if not await _verify_channel_membership(
            channel_id, user_id, "voice_message_sent"
        ):
            logger.warning(
                "voice_message_sent_unauthorized",
                sid=sid,
                user_id=user_id,
                channel_id=channel_id,
            )
            return

        # Broadcast to channel
        broadcast_data = {
            "channel_id": channel_id,
            "voice_message_id": voice_message_id,
            "sender_id": user_id,
            "duration_ms": data.get("duration_ms", 0),
            "mime_type": data.get("mime_type", "audio/mpeg"),
            "waveform_data": data.get("waveform_data", []),
            "created_at": data.get("created_at", datetime.utcnow().isoformat()),
        }

        # Local room broadcast first (skip_sid excludes the sender's
        # own session). Cross-server delivery via fabric_emit so peers
        # on sibling servers see it without being in the local room.
        await sio.emit(
            "voice_message_sent",
            broadcast_data,
            room=channel_id,
            skip_sid=sid,
        )

        from app.services import fabric_emit as _fe
        await _fe.emit_broadcast(
            event_type="voice_message_sent",
            priority="P2",
            payload=broadcast_data,
            channel_id=channel_id,
            source_user_id=user_id,
            idempotency_key=f"voice_msg:{voice_message_id}",
        )

        logger.info(
            "voice_message_sent_broadcast",
            voice_message_id=voice_message_id,
            channel_id=channel_id,
            user_id=user_id,
        )

    except Exception as e:
        logger.error(
            "voice_message_sent_error",
            error=str(e),
            sid=sid,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Voice Message Playing (Presence)
# ─────────────────────────────────────────────────────────────────────────────

# Track active playback: (user_id, channel_id) -> {voice_message_id, start_time}
_active_playback: dict[tuple[str, str], dict] = {}


@sio.event
async def voice_message_playing(sid: str, data: dict):
    """
    Notify channel that user started playing a voice message.

    Used to show "User is listening to voice message #..." presence indicator.

    data: {
        channel_id: str,
        voice_message_id: str,
    }
    """
    try:
        user_id = await get_user_id(sid)
        if not user_id:
            logger.warning("voice_message_playing_no_user", sid=sid)
            return

        channel_id = data.get("channel_id")
        voice_message_id = data.get("voice_message_id")

        if not channel_id or not voice_message_id:
            logger.warning(
                "voice_message_playing_invalid_data",
                sid=sid,
                user_id=user_id,
            )
            return

        # Verify membership
        if not await _verify_channel_membership(
            channel_id, user_id, "voice_message_playing"
        ):
            return

        # Track playback
        key = (user_id, channel_id)
        _active_playback[key] = {
            "voice_message_id": voice_message_id,
            "start_time": datetime.utcnow().isoformat(),
        }

        # Broadcast presence
        await sio.emit(
            "voice_message_playing",
            {
                "user_id": user_id,
                "channel_id": channel_id,
                "voice_message_id": voice_message_id,
            },
            room=channel_id,
            skip_sid=sid,
        )

        logger.info(
            "voice_message_playing",
            user_id=user_id,
            channel_id=channel_id,
            voice_message_id=voice_message_id,
        )

    except Exception as e:
        logger.error(
            "voice_message_playing_error",
            error=str(e),
            sid=sid,
        )


@sio.event
async def voice_message_stopped(sid: str, data: dict):
    """
    Notify channel that user stopped playing voice message.

    data: {
        channel_id: str,
        voice_message_id: str,
    }
    """
    try:
        user_id = await get_user_id(sid)
        if not user_id:
            return

        channel_id = data.get("channel_id")
        voice_message_id = data.get("voice_message_id")

        if not channel_id:
            return

        # Remove from tracking
        key = (user_id, channel_id)
        if key in _active_playback:
            del _active_playback[key]

        # Broadcast presence update
        await sio.emit(
            "voice_message_stopped",
            {
                "user_id": user_id,
                "channel_id": channel_id,
                "voice_message_id": voice_message_id,
            },
            room=channel_id,
            skip_sid=sid,
        )

        logger.info(
            "voice_message_stopped",
            user_id=user_id,
            channel_id=channel_id,
        )

    except Exception as e:
        logger.error(
            "voice_message_stopped_error",
            error=str(e),
            sid=sid,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Voice Message Deleted
# ─────────────────────────────────────────────────────────────────────────────


@sio.event
async def voice_message_deleted(sid: str, data: dict):
    """
    Notify channel when voice message is deleted.

    data: {
        channel_id: str,
        voice_message_id: str,
    }
    """
    try:
        user_id = await get_user_id(sid)
        if not user_id:
            return

        channel_id = data.get("channel_id")
        voice_message_id = data.get("voice_message_id")

        if not channel_id or not voice_message_id:
            return

        # Verify membership
        if not await _verify_channel_membership(
            channel_id, user_id, "voice_message_deleted"
        ):
            return

        # Broadcast deletion
        await sio.emit(
            "voice_message_deleted",
            {
                "channel_id": channel_id,
                "voice_message_id": voice_message_id,
            },
            room=channel_id,
            skip_sid=sid,
        )

        logger.info(
            "voice_message_deleted_broadcast",
            voice_message_id=voice_message_id,
            channel_id=channel_id,
            user_id=user_id,
        )

    except Exception as e:
        logger.error(
            "voice_message_deleted_error",
            error=str(e),
            sid=sid,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup on Disconnect
# ─────────────────────────────────────────────────────────────────────────────


def cleanup_voice_playback(sid: str):
    """Clean up voice message playback state on disconnect.

    Called from the main disconnect handler in server.py to avoid
    registering a duplicate @sio.event disconnect that would silently
    overwrite the primary handler.
    """
    # Resolve user_id from sid via the presence service, since
    # _active_playback keys are (user_id, channel_id) tuples — not sid.
    from app.services.presence_service import presence_service as _ps
    user_id = _ps.get_user_id(sid)
    if not user_id:
        return
    # Find and remove all playback entries for this user
    keys_to_remove = [key for key in _active_playback.keys() if key[0] == user_id]
    for key in keys_to_remove:
        del _active_playback[key]
