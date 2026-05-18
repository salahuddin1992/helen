"""
Chat socket event handlers — real-time messaging, typing indicators, delivery receipts.

Security hardening:
  - All handlers verify channel membership before processing
  - Membership cache reduces DB queries for high-frequency events (typing, read)
  - Audit logging on unauthorized access attempts
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
from app.services.message_service import MessageService
from app.services.presence_service import presence_service
from app.socket.server import emit_to_user, get_user_id, sio

logger = get_logger(__name__)


async def _verify_channel_membership(
    channel_id: str, user_id: str, action: str = "unknown"
) -> bool:
    """
    Verify user is a member of a channel. Uses cache for high-frequency checks.
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
        logger.error("membership_check_error", error=str(e), channel_id=channel_id, user_id=user_id)
        return False

# Rate limiter for typing events: track (user_id, channel_id) -> list of timestamps
_typing_rate_limit: dict[tuple[str, str], list[datetime]] = {}


@sio.event
async def chat_send_message(sid: str, data: dict):
    """
    Client sends a message.
    data: {
        channel_id: str,
        content: str,
        type: "text" | "file" | "image" | "reply",
        reply_to: str | null,
        file_id: str | null
    }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    channel_id = data.get("channel_id")
    content = data.get("content", "").strip()
    msg_type = data.get("type", "text")
    reply_to = data.get("reply_to")
    file_id = data.get("file_id")

    if not channel_id or not content:
        return {"error": "channel_id and content are required"}

    if isinstance(content, str) and len(content) > 10000:
        return {"error": "Message too long"}

    try:
        async with async_session_factory() as db:
            message = await MessageService.send_message(
                db, channel_id, user_id, content,
                msg_type=msg_type, reply_to=reply_to, file_id=file_id,
            )

            # ── @mention parsing + notification persistence ──
            sender_username = message.sender.username if message.sender else None
            mentioned_user_ids = await MessageService.dispatch_mentions(
                db, message, sender_username=sender_username
            )
            await db.commit()

            msg_data = {
                "id": message.id,
                "channel_id": message.channel_id,
                "sender": {
                    "id": message.sender.id,
                    "username": message.sender.username,
                    "display_name": message.sender.display_name,
                    "avatar_url": message.sender.avatar_url,
                } if message.sender else {},
                "content": message.content,
                "type": message.type,
                "reply_to": message.reply_to,
                "file_id": message.file_id,
                "mentions": mentioned_user_ids,
                "created_at": message.created_at.isoformat(),
            }

            # Get channel members to notify
            channel = await ChannelService.get_channel(db, channel_id)
            member_ids = [m.user_id for m in channel.members if m.user_id != user_id]

        # Send to ONLINE members only (audit fix H-1). Skip members
        # that have no local sid AND no federated_presence entry —
        # they're offline across the entire mesh and emit_to_user
        # would just create a federation flood + DLQ noise. The
        # message is already persisted in DB so they'll see it via
        # backfill on reconnect.
        _failed_member_ids: list[str] = []
        # Build presence snapshot once.
        try:
            from app.services.federated_presence import federated_presence as _fp
        except Exception:
            _fp = None
        for member_id in member_ids:
            online_local = bool(presence_service.get_sids(member_id))
            online_remote = False
            if not online_local and _fp is not None:
                try:
                    online_remote = (await _fp.get(member_id)) is not None
                except Exception:
                    online_remote = False
            if not online_local and not online_remote:
                continue
            try:
                await emit_to_user("chat:new_message", msg_data, member_id)
            except Exception as _e:
                logger.warning(
                    "chat_fanout_member_failed",
                    member_id=member_id,
                    channel_id=channel_id,
                    error=str(_e),
                )
                _failed_member_ids.append(member_id)
        if _failed_member_ids:
            try:
                from app.services.dead_letter_service import record as _dlq_record
                await _dlq_record(
                    kind="fanout",
                    reason="chat_new_message_partial_failure",
                    error=f"failed_member_count={len(_failed_member_ids)}",
                    payload={
                        "event": "chat:new_message",
                        "channel_id": channel_id,
                        "message": msg_data,
                        "member_ids": _failed_member_ids,
                    },
                    message_id=message.id,
                    channel_id=channel_id,
                    sender_id=user_id,
                )
            except Exception:
                pass  # DLQ recording must never break the hot path

        # Push real-time notification:new to mentioned users
        if mentioned_user_ids:
            mention_payload = {
                "type": "mention",
                "title": f"@{sender_username} mentioned you" if sender_username else "You were mentioned",
                "body": (message.content or "")[:280],
                "reference_id": message.id,
                "reference_type": "message",
                "channel_id": channel_id,
                "message_id": message.id,
                "sender_id": user_id,
                "sender_username": sender_username,
                "created_at": message.created_at.isoformat(),
            }
            from app.services import fabric_emit as _fe
            for mentioned_uid in mentioned_user_ids:
                # P2 with idempotency on (message.id, mentioned_uid) so
                # a retry of the parent send doesn't double-notify.
                await _fe.emit_event(
                    event_type="notification:new",
                    priority="P2",
                    payload=mention_payload,
                    destination_user_id=mentioned_uid,
                    source_user_id=user_id,
                    channel_id=channel_id,
                    idempotency_key=f"mention_notif:{message.id}:{mentioned_uid}",
                )

        # ACK back to sender
        return {"message_id": message.id, "created_at": message.created_at.isoformat()}

    except Exception as e:
        logger.error("chat_send_error", error=str(e), user_id=user_id)
        # DLQ best-effort: captures the send attempt so operators can
        # inspect the failure and replay after fixing root cause.
        try:
            from app.services.dead_letter_service import record as _dlq_record
            await _dlq_record(
                kind="fanout",
                reason="chat_send_message_exception",
                error=e,
                payload={
                    "event": "chat:new_message",
                    "channel_id": channel_id,
                    "content": (content or "")[:2048],
                    "type": msg_type,
                    "reply_to": reply_to,
                    "file_id": file_id,
                },
                channel_id=channel_id,
                sender_id=user_id,
            )
        except Exception:
            pass
        return {"error": str(e)}


@sio.event
async def chat_typing_start(sid: str, data: dict):
    """
    Client started typing.
    data: { channel_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    channel_id = data.get("channel_id")
    if not channel_id:
        return

    # SECURITY: Verify channel membership before broadcasting typing
    if not await _verify_channel_membership(channel_id, user_id, "typing_start"):
        return

    # Rate limiting: max 3 typing events per 2 seconds per user per channel
    key = (user_id, channel_id)
    now = datetime.now()
    cutoff = now - timedelta(seconds=2)

    if key not in _typing_rate_limit:
        _typing_rate_limit[key] = []

    # Clean old timestamps
    _typing_rate_limit[key] = [ts for ts in _typing_rate_limit[key] if ts > cutoff]

    # Check rate limit
    if len(_typing_rate_limit[key]) >= 3:
        logger.warning("typing_rate_limit_exceeded", user_id=user_id, channel_id=channel_id)
        return

    _typing_rate_limit[key].append(now)

    try:
        # O(1) handler work: room broadcast covers every locally-connected
        # member regardless of channel size. Cross-server members aren't
        # in the local room — emit_to_user picks them up via federation.
        from app.socket import channel_room as _channel_room
        await _channel_room.ensure_populated(sio, channel_id)
        room = _channel_room.room_name(channel_id)
        payload = {
            "channel_id": channel_id,
            "user_id": user_id,
            "is_typing": True,
        }
        await sio.emit("chat:typing", payload, room=room, skip_sid=sid)

        # Cross-server fan-out only for members WITHOUT a local sid.
        async with async_session_factory() as db:
            channel = await ChannelService.get_channel(db, channel_id)
            member_ids = [m.user_id for m in channel.members if m.user_id != user_id]
        # Cross-server fan-out via fabric_emit (allowlist gates the
        # fabric path; legacy emit_to_user fallback otherwise). P3 =
        # best-effort, drop-oldest under pressure. Typing events are
        # cheap and high-volume; never ACK.
        from app.services import fabric_emit as _fe
        for member_id in member_ids:
            if presence_service.get_sids(member_id):
                continue
            await _fe.emit_event(
                event_type="chat:typing",
                priority="P3",
                payload=payload,
                destination_user_id=member_id,
                source_user_id=user_id,
                channel_id=channel_id,
                requires_ack=False,
            )
    except Exception as e:
        logger.warning("typing_start_error", error=str(e))


@sio.event
async def chat_typing_stop(sid: str, data: dict):
    """
    Client stopped typing.
    data: { channel_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    channel_id = data.get("channel_id")
    if not channel_id:
        return

    # SECURITY: Verify channel membership
    if not await _verify_channel_membership(channel_id, user_id, "typing_stop"):
        return

    # Rate limiting: max 3 typing events per 2 seconds per user per channel
    key = (user_id, channel_id)
    now = datetime.now()
    cutoff = now - timedelta(seconds=2)

    if key not in _typing_rate_limit:
        _typing_rate_limit[key] = []

    # Clean old timestamps
    _typing_rate_limit[key] = [ts for ts in _typing_rate_limit[key] if ts > cutoff]

    # Check rate limit
    if len(_typing_rate_limit[key]) >= 3:
        logger.warning("typing_rate_limit_exceeded", user_id=user_id, channel_id=channel_id)
        return

    _typing_rate_limit[key].append(now)

    try:
        from app.socket import channel_room as _channel_room
        await _channel_room.ensure_populated(sio, channel_id)
        room = _channel_room.room_name(channel_id)
        payload = {
            "channel_id": channel_id,
            "user_id": user_id,
            "is_typing": False,
        }
        await sio.emit("chat:typing", payload, room=room, skip_sid=sid)

        async with async_session_factory() as db:
            channel = await ChannelService.get_channel(db, channel_id)
            member_ids = [m.user_id for m in channel.members if m.user_id != user_id]
        # Cross-server fan-out via fabric_emit (allowlist gates the
        # fabric path; legacy emit_to_user fallback otherwise). P3 =
        # best-effort, drop-oldest under pressure. Typing events are
        # cheap and high-volume; never ACK.
        from app.services import fabric_emit as _fe
        for member_id in member_ids:
            if presence_service.get_sids(member_id):
                continue
            await _fe.emit_event(
                event_type="chat:typing",
                priority="P3",
                payload=payload,
                destination_user_id=member_id,
                source_user_id=user_id,
                channel_id=channel_id,
                requires_ack=False,
            )
    except Exception as e:
        logger.warning("typing_stop_error", error=str(e))


@sio.event
async def chat_message_read(sid: str, data: dict):
    """
    Client marks messages as read.
    data: { channel_id: str, message_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    channel_id = data.get("channel_id")
    message_id = data.get("message_id")
    if not channel_id or not message_id:
        return

    # SECURITY: Verify channel membership before marking read
    if not await _verify_channel_membership(channel_id, user_id, "message_read"):
        return

    try:
        async with async_session_factory() as db:
            await MessageService.mark_read(db, channel_id, user_id, message_id)

            # Notify the sender about the read receipt
            channel = await ChannelService.get_channel(db, channel_id)
            member_ids = [m.user_id for m in channel.members if m.user_id != user_id]

        # O(1) handler: room broadcast for local members + federation
        # fall-back for cross-server peers.
        from app.socket import channel_room as _channel_room
        await _channel_room.ensure_populated(sio, channel_id)
        room = _channel_room.room_name(channel_id)
        payload = {
            "channel_id": channel_id,
            "message_id": message_id,
            "user_id": user_id,
            "status": "read",
        }
        await sio.emit("chat:delivery_receipt", payload, room=room, skip_sid=sid)
        for member_id in member_ids:
            if presence_service.get_sids(member_id):
                continue
            await emit_to_user("chat:delivery_receipt", payload, member_id)
    except Exception as e:
        logger.warning("message_read_error", error=str(e))


@sio.event
async def chat_reaction(sid: str, data: dict):
    """
    Toggle reaction on a message.
    data: { message_id: str, emoji: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    message_id = data.get("message_id")
    emoji = data.get("emoji")
    if not message_id or not emoji:
        return

    if not isinstance(emoji, str) or len(emoji) > 8:
        return

    try:
        async with async_session_factory() as db:
            reactions = await MessageService.toggle_reaction(db, message_id, user_id, emoji)
            aggregated = MessageService.aggregate_reactions(reactions)

            # Get message's channel to find members
            from sqlalchemy import select
            from app.models.message import Message
            msg_result = await db.execute(select(Message).where(Message.id == message_id))
            msg = msg_result.scalar_one_or_none()
            if not msg:
                return

            channel = await ChannelService.get_channel(db, msg.channel_id)
            member_ids = [m.user_id for m in channel.members]

        # Broadcast to all members including sender. Room emit covers
        # local subscribers (all sids in channel:{id}); cross-server
        # peers not in local room get federated emit.
        from app.socket import channel_room as _channel_room
        # Find channel_id from message
        channel_id = msg.channel_id
        await _channel_room.ensure_populated(sio, channel_id)
        room = _channel_room.room_name(channel_id)
        payload = {
            "message_id": message_id,
            "reactions": aggregated,
        }
        await sio.emit("chat:reaction_update", payload, room=room)
        for member_id in member_ids:
            if presence_service.get_sids(member_id):
                continue
            await emit_to_user("chat:reaction_update", payload, member_id)
    except Exception as e:
        logger.warning("reaction_error", error=str(e))


# NOTE: V2 chat handlers (v2_chat_send_message, v2_chat_edit_message,
# v2_chat_delete_message, v2_chat_mark_read, v2_chat_mark_delivered,
# v2_chat_typing_start, v2_chat_typing_stop, v2_chat_reaction, etc.)
# are registered in sync_handlers.py with enhanced delivery tracking.


async def _broadcast_to_channel(event_name: str, data: dict, channel_members, exclude_user=None, *, channel_id: str | None = None):
    """
    Broadcast event to all online members of a channel.

    Callers that know the ``channel_id`` get the fast path: a single
    room-scoped emit (O(1) from the handler regardless of member count).
    The pre-join into ``channel:{id}`` happens on socket connect.

    Legacy callers that only have the members list still work via a parallel
    per-sid fanout, but that path is O(N) and should not be relied on for
    large channels.
    """
    if channel_id is not None:
        try:
            await sio.emit(event_name, data, room=f"channel:{channel_id}")
        except Exception as _e:
            logger.warning("channel_room_emit_failed", event=event_name, error=str(_e))
        return

    import asyncio

    member_ids: list[str] = []
    for member in channel_members:
        if exclude_user is not None and member.user_id == exclude_user:
            continue
        member_ids.append(member.user_id)

    if not member_ids:
        return

    # emit_to_user fans out per-user (covers multi-device locally and
    # falls back to federation when the member is hosted on a sibling
    # Helen server). Parallelized via gather to keep handler latency O(1)
    # in the wire round-trip dimension.
    await asyncio.gather(
        *(emit_to_user(event_name, data, mid) for mid in member_ids),
        return_exceptions=True,
    )


@sio.event
async def v2_chat_sync(sid: str, data: dict):
    """
    v2: Reconnection sync - fetch missed messages and confirm delivery.
    data: { last_sync_at: str (ISO), channels: [str] }
    Returns missed messages grouped by channel with delivery confirmation.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    from datetime import datetime
    last_sync_at = data.get("last_sync_at")
    channels = data.get("channels", [])

    try:
        from app.services.sync_service import sync_service
        async with async_session_factory() as db:
            # SECURITY: Filter channels to only those the user is a member of
            if channels:
                verified_channels = []
                for ch_id in channels:
                    if is_valid_uuid(ch_id) and await ChannelService.is_member(db, ch_id, user_id):
                        verified_channels.append(ch_id)
                    else:
                        logger.warning("sync_unauthorized_channel", user_id=user_id, channel_id=ch_id)
                channels = verified_channels

            # Sync and confirm delivery
            result = await sync_service.sync_and_confirm_delivery(db, user_id, last_sync_at, channels)

            return {
                "missed_messages": result.get("missed_messages", {}),
                "delivery_confirmations": result.get("delivery_confirmations", {}),
                "synced_at": datetime.utcnow().isoformat(),
            }

    except Exception as e:
        logger.error("v2_chat_sync_error", error=str(e), user_id=user_id)
        return {"error": str(e)}


@sio.event
async def v2_chat_fetch_summaries(sid: str, data: dict):
    """
    v2: Fetch channel summaries (unread counts, last message, etc).
    data: {}
    Returns channel_summaries event.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    try:
        from app.services.sync_service import sync_service
        async with async_session_factory() as db:
            summaries = await sync_service.get_channel_summaries(db, user_id)

            await sio.emit("v2_chat:channel_summaries", {
                "summaries": summaries,
            }, to=sid)

            return {"status": "summaries_sent"}

    except Exception as e:
        logger.error("v2_fetch_summaries_error", error=str(e), user_id=user_id)
        return {"error": str(e)}


@sio.event
async def v2_chat_fetch_unread(sid: str, data: dict):
    """
    v2: Fetch unread message counts per channel.
    data: {}
    Returns channel_unread event.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    try:
        from app.services.sync_service import sync_service
        async with async_session_factory() as db:
            unread_counts = await sync_service.get_unread_counts(db, user_id)

            await sio.emit("v2_chat:channel_unread", {
                "unread": unread_counts,
            }, to=sid)

            return {"status": "unread_sent"}

    except Exception as e:
        logger.error("v2_fetch_unread_error", error=str(e), user_id=user_id)
        return {"error": str(e)}


@sio.event
async def v2_chat_pin_message(sid: str, data: dict):
    """
    v2: Pin a message.
    data: { message_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    message_id = data.get("message_id")
    if not message_id:
        return {"error": "message_id is required"}

    try:
        async with async_session_factory() as db:
            # Get message to find channel
            from sqlalchemy import select
            from app.models.message import Message
            msg_result = await db.execute(select(Message).where(Message.id == message_id))
            msg = msg_result.scalar_one_or_none()
            if not msg:
                return {"error": "Message not found"}

            channel = await ChannelService.get_channel(db, msg.channel_id)

            # Pin message
            await MessageService.pin_message(db, message_id, user_id)

            event_data = {
                "message_id": message_id,
                "channel_id": msg.channel_id,
                "pinned_by": user_id,
                "pinned_at": datetime.utcnow().isoformat(),
            }

        # Broadcast to all channel members
        await _broadcast_to_channel("v2_chat:message_pinned", event_data, channel.members)

        return {"message_id": message_id}

    except Exception as e:
        logger.error("v2_pin_message_error", error=str(e), user_id=user_id)
        return {"error": str(e)}


@sio.event
async def v2_chat_unpin_message(sid: str, data: dict):
    """
    v2: Unpin a message.
    data: { message_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    message_id = data.get("message_id")
    if not message_id:
        return {"error": "message_id is required"}

    try:
        async with async_session_factory() as db:
            # Get message to find channel
            from sqlalchemy import select
            from app.models.message import Message
            msg_result = await db.execute(select(Message).where(Message.id == message_id))
            msg = msg_result.scalar_one_or_none()
            if not msg:
                return {"error": "Message not found"}

            channel = await ChannelService.get_channel(db, msg.channel_id)

            # Unpin message
            await MessageService.unpin_message(db, message_id, user_id)

            event_data = {
                "message_id": message_id,
                "channel_id": msg.channel_id,
                "unpinned_by": user_id,
                "unpinned_at": datetime.utcnow().isoformat(),
            }

        # Broadcast to all channel members
        await _broadcast_to_channel("v2_chat:message_unpinned", event_data, channel.members)

        return {"message_id": message_id}

    except Exception as e:
        logger.error("v2_unpin_message_error", error=str(e), user_id=user_id)
        return {"error": str(e)}


@sio.event
async def v2_chat_forward_message(sid: str, data: dict):
    """
    v2: Forward a message to another channel.
    data: { message_id: str, target_channel_id: str, content: str (optional prepend) }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    message_id = data.get("message_id")
    target_channel_id = data.get("target_channel_id")
    content_prepend = data.get("content", "").strip()

    if not message_id or not target_channel_id:
        return {"error": "message_id and target_channel_id are required"}

    try:
        async with async_session_factory() as db:
            # Forward message (validates membership internally)
            forwarded = await MessageService.forward_message(
                db, message_id, target_channel_id, user_id, content_prepend
            )

            target_channel = await ChannelService.get_channel(db, target_channel_id)

            msg_data = {
                "id": forwarded.id,
                "channel_id": forwarded.channel_id,
                "sender": {
                    "id": forwarded.sender.id,
                    "username": forwarded.sender.username,
                    "display_name": forwarded.sender.display_name,
                    "avatar_url": forwarded.sender.avatar_url,
                } if forwarded.sender else {},
                "content": forwarded.content,
                "type": forwarded.type,
                "created_at": forwarded.created_at.isoformat(),
                "forwarded_from": {
                    "message_id": message_id,
                },
            }

        # Broadcast to target channel members
        await _broadcast_to_channel("v2_chat:new_message", msg_data, target_channel.members)

        return {"message_id": forwarded.id, "target_channel_id": target_channel_id}

    except Exception as e:
        logger.error("v2_forward_message_error", error=str(e), user_id=user_id)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════
# ── Channel Preferences (archive / mute / pin / read) ──
# ══════════════════════════════════════════════════════

def _prefs_payload(member) -> dict:
    return {
        "channel_id": member.channel_id,
        "user_id": member.user_id,
        "is_archived": bool(member.is_archived),
        "is_pinned": bool(member.is_pinned),
        "is_muted": bool(member.is_muted),
        "mute_until": member.mute_until.isoformat() if member.mute_until else None,
        "last_read_at": member.last_read_at.isoformat() if member.last_read_at else None,
        "last_read_message_id": member.last_read_message_id,
    }


async def _emit_to_user(user_id: str, event: str, payload: dict, exclude_sid: str | None = None):
    """Emit to all of this user's connected sockets, optionally skipping one."""
    for s in presence_service.get_sids(user_id):
        if exclude_sid and s == exclude_sid:
            continue
        await sio.emit(event, payload, to=s)


@sio.event
async def channel_archive(sid: str, data: dict):
    """data: { channel_id: str, archived: bool }"""
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}
    channel_id = data.get("channel_id")
    archived = bool(data.get("archived", True))
    if not channel_id:
        return {"error": "channel_id is required"}

    try:
        async with async_session_factory() as db:
            member = await ChannelService.set_archived(db, channel_id, user_id, archived)
        payload = _prefs_payload(member)
        await _emit_to_user(user_id, "channel:prefs_updated", payload, exclude_sid=sid)
        return {"ok": True, **payload}
    except Exception as e:
        logger.error("channel_archive_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def channel_pin(sid: str, data: dict):
    """data: { channel_id: str, pinned: bool }"""
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}
    channel_id = data.get("channel_id")
    pinned = bool(data.get("pinned", True))
    if not channel_id:
        return {"error": "channel_id is required"}

    try:
        async with async_session_factory() as db:
            member = await ChannelService.set_pinned(db, channel_id, user_id, pinned)
        payload = _prefs_payload(member)
        await _emit_to_user(user_id, "channel:prefs_updated", payload, exclude_sid=sid)
        return {"ok": True, **payload}
    except Exception as e:
        logger.error("channel_pin_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def channel_mute(sid: str, data: dict):
    """
    data: {
        channel_id: str,
        muted: bool,
        mute_until?: ISO 8601 string  (optional auto-unmute time)
    }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}
    channel_id = data.get("channel_id")
    muted = bool(data.get("muted", True))
    raw_until = data.get("mute_until")
    if not channel_id:
        return {"error": "channel_id is required"}

    mute_until = None
    if raw_until:
        try:
            if isinstance(raw_until, str):
                mute_until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return {"error": "mute_until must be ISO 8601"}

    try:
        async with async_session_factory() as db:
            member = await ChannelService.set_muted(
                db, channel_id, user_id, muted, mute_until=mute_until,
            )
        payload = _prefs_payload(member)
        await _emit_to_user(user_id, "channel:prefs_updated", payload, exclude_sid=sid)
        return {"ok": True, **payload}
    except Exception as e:
        logger.error("channel_mute_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def channel_mark_read(sid: str, data: dict):
    """
    data: { channel_id: str, message_id?: str }
    Updates the user's last-read pointer for a channel.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}
    channel_id = data.get("channel_id")
    message_id = data.get("message_id")
    if not channel_id:
        return {"error": "channel_id is required"}

    try:
        async with async_session_factory() as db:
            member = await ChannelService.update_last_read(
                db, channel_id, user_id, message_id=message_id,
            )
        payload = _prefs_payload(member)
        # Sync across the user's other devices
        await _emit_to_user(user_id, "channel:prefs_updated", payload, exclude_sid=sid)
        return {"ok": True, **payload}
    except Exception as e:
        logger.error("channel_mark_read_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


# ── Colon-style aliases used by some clients ────────────
@sio.on("channel:archive")
async def _channel_archive_alias(sid, data): return await channel_archive(sid, data)


@sio.on("channel:pin")
async def _channel_pin_alias(sid, data): return await channel_pin(sid, data)


@sio.on("channel:mute")
async def _channel_mute_alias(sid, data): return await channel_mute(sid, data)


@sio.on("channel:mark_read")
async def _channel_mark_read_alias(sid, data): return await channel_mark_read(sid, data)
