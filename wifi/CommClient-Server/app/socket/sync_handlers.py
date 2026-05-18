"""
Sync socket handlers — reconnection sync, delivery receipts, unread counts.

These handlers operate alongside the existing chat_handlers.py.
The v2 chat handlers below use the SyncService for enhanced delivery tracking
and reconnection sync capabilities.

Socket Events (client → server):
  sync_request         — Client reconnected; requests missed messages since timestamp
  sync_unread_counts   — Client requests current unread counts per channel
  sync_channel_summaries — Client requests channel list summaries
  v2_chat_send_message — Enhanced send with receipt creation
  v2_chat_mark_delivered — Mark specific messages as delivered
  v2_chat_mark_read    — Mark channel messages as read (with proper receipts)
  v2_chat_message_read — Mark up to a specific message as read
  v2_chat_edit_message — Edit a message and broadcast
  v2_chat_delete_message — Soft-delete and broadcast

Socket Events (server → client):
  sync_missed_messages   — Batch of missed messages grouped by channel
  sync_unread_counts     — Per-channel unread counts + last message preview
  sync_channel_summaries — Channel list summaries
  v2_chat:new_message    — New message with receipt tracking
  v2_chat:message_delivered — Delivery receipt notification to sender
  v2_chat:message_read     — Read receipt notification to sender
  v2_chat:message_edited   — Edited message broadcast
  v2_chat:message_deleted  — Deleted message broadcast
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

# ── Per-channel broadcast sequence ──
# Monotonic counter per channel that the client uses to detect gaps in
# the realtime stream and decide whether a `sync_request` is needed
# even though the socket reports connected. In-memory and process-local
# — sequences DON'T need to be globally consistent across servers; the
# client always pairs the seq with `channel_id` so cross-server
# duplicates resolve via the existing message.id dedup. For full
# multi-server consistency, swap the dict for Redis INCR keyed by
# (server_id, channel_id) — the API surface is identical.
_channel_seq: dict[str, int] = {}
_channel_seq_lock = asyncio.Lock()


async def _next_channel_seq(channel_id: str) -> int:
    async with _channel_seq_lock:
        n = _channel_seq.get(channel_id, 0) + 1
        _channel_seq[channel_id] = n
        return n

from app.core.audit import audit_permission_denied
from app.core.logging import get_logger
from app.core.security_utils import (
    cache_membership,
    get_cached_membership,
    is_valid_uuid,
)
from app.db.session import async_session_factory
from app.models.message_status import MessageReceipt
from app.services.call_service import call_service
from app.services.channel_service import ChannelService
from app.services.message_service import MessageService
from app.services.presence_service import presence_service
from app.services.sync_service import sync_service
from app.socket.rate_limiter import socket_rate_limiter
from app.socket.server import get_user_id, sio

logger = get_logger(__name__)


def _rate_check(user_id: str, event: str) -> bool:
    """Check rate limit. Returns True if allowed."""
    return socket_rate_limiter.check(user_id, event)


async def _verify_channel_membership(
    channel_id: str, user_id: str, action: str = "unknown"
) -> bool:
    """
    Verify user is a member of a channel. Uses cache for high-frequency checks.
    Returns True if member, False if not. Logs unauthorized attempts.
    """
    if not is_valid_uuid(channel_id) or not is_valid_uuid(user_id):
        return False

    cached = get_cached_membership(channel_id, user_id)
    if cached is not None:
        if not cached:
            audit_permission_denied(user_id, f"channel:{channel_id}", action)
        return cached

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


# Rate limiter for v2 typing events: (user_id, channel_id) -> list of timestamps
_v2_typing_rate_limit: dict[tuple[str, str], list[datetime]] = {}


# ══════════════════════════════════════════════════════
# ── Reconnection Sync Events ─────────────────────────
# ══════════════════════════════════════════════════════


@sio.event
async def sync_request(sid: str, data: dict):
    """
    Client reconnected and requests all missed messages.
    data: {
        since: str (ISO 8601 timestamp — last known message time),
        limit?: int (default 500)
    }
    Returns: { channels: { channel_id: [messages] }, unread: { channel_id: {unread, last_message} } }
    """
    start = time.monotonic()
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    since_str = data.get("since")
    if not since_str:
        return {"error": "since timestamp is required"}

    try:
        since = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return {"error": "Invalid timestamp format. Use ISO 8601."}

    limit = min(data.get("limit", 500), 1000)

    try:
        async with async_session_factory() as db:
            # Get missed messages
            missed = await sync_service.get_missed_messages(
                db, user_id, since, limit
            )

            # Get current unread counts
            unread = await sync_service.get_unread_counts(db, user_id)

            # Bulk mark all missed messages as delivered
            all_msg_ids = []
            for msgs in missed.values():
                for msg in msgs:
                    if msg.get("sender", {}).get("id") != user_id:
                        all_msg_ids.append(msg["id"])

            if all_msg_ids:
                delivered_count = await sync_service.bulk_mark_delivered(
                    db, user_id, message_ids=all_msg_ids
                )
                await db.commit()

                # Notify senders of delivery
                await _notify_delivered_bulk(all_msg_ids, user_id, missed)
            else:
                await db.commit()

        total_msgs = sum(len(v) for v in missed.values())
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "sync_completed",
            user_id=user_id,
            since=since_str,
            channels=len(missed),
            messages=total_msgs,
            duration_ms=duration_ms,
        )

        return {
            "channels": missed,
            "unread": unread,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error("sync_request_error", user_id=user_id, error=str(e), duration_ms=duration_ms)
        return {"error": "Sync failed"}


@sio.event
async def sync_unread_counts(sid: str, data: dict):
    """
    Request current unread counts per channel.
    data: {} (no parameters needed)
    Returns: { channel_id: { unread: int, last_message: {...} } }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    try:
        async with async_session_factory() as db:
            unread = await sync_service.get_unread_counts(db, user_id)
        return {"unread": unread}
    except Exception as e:
        logger.error("sync_unread_error", user_id=user_id, error=str(e))
        return {"error": "Failed to fetch unread counts"}


@sio.event
async def sync_channel_summaries(sid: str, data: dict):
    """
    Request channel list summaries (unread + last message per channel).
    data: {} (no parameters needed)
    Returns: { summaries: [...] }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    try:
        async with async_session_factory() as db:
            summaries = await sync_service.get_channel_summaries(db, user_id)
        return {"summaries": summaries}
    except Exception as e:
        logger.error("sync_summaries_error", user_id=user_id, error=str(e))
        return {"error": "Failed to fetch summaries"}


# ══════════════════════════════════════════════════════
# ── V2 Chat — Enhanced Messaging ─────────────────────
# ══════════════════════════════════════════════════════


@sio.event
async def v2_chat_send_message(sid: str, data: dict):
    """
    Enhanced message send — creates message + delivery receipts.
    data: {
        channel_id: str,
        content: str,
        type?: "text" | "file" | "image" | "reply",
        reply_to?: str,
        file_id?: str,
        client_id?: str  (client-side temp ID for deduplication)
    }
    Returns: { message_id, client_id, created_at } or { error }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    if not _rate_check(user_id, "v2_chat_send_message"):
        return {"error": "Rate limited — slow down"}

    channel_id = data.get("channel_id")
    content = data.get("content", "").strip() if isinstance(data.get("content"), str) else ""
    msg_type = data.get("type", "text")
    reply_to = data.get("reply_to")
    file_id = data.get("file_id")
    client_id = data.get("client_id")  # For optimistic UI dedup

    # ── Input Validation ─────────────────────────────
    if not channel_id or not isinstance(channel_id, str):
        return {"error": "channel_id is required"}
    if not content and not file_id:
        return {"error": "content or file_id is required"}
    if len(content) > 10000:
        return {"error": "Message content exceeds maximum length (10000 chars)"}
    if msg_type not in ("text", "file", "image", "reply", "system"):
        return {"error": f"Invalid message type: {msg_type}"}
    if reply_to and not isinstance(reply_to, str):
        return {"error": "Invalid reply_to format"}
    if file_id and not isinstance(file_id, str):
        return {"error": "Invalid file_id format"}

    try:
        async with async_session_factory() as db:
            # Verify membership
            is_member = await ChannelService.is_member(db, channel_id, user_id)
            if not is_member:
                return {"error": "Not a member of this channel"}

            # MessageService.send_message already creates delivery receipts
            # internally — do not create them a second time or the
            # UNIQUE(message_id, recipient_id) constraint will fire.
            message = await MessageService.send_message(
                db, channel_id, user_id, content, msg_type, reply_to, file_id
            )

            # ── @mention parsing + notification persistence ──
            sender_username = message.sender.username if message.sender else None
            mentioned_user_ids = await MessageService.dispatch_mentions(
                db, message, sender_username=sender_username
            )

            await db.commit()

            # Per-channel monotonic sequence — clients use it to detect
            # dropped messages on the realtime socket and trigger a
            # `sync_request` if the seq jumps. NOT a global sequence —
            # only valid within a (server, channel) tuple.
            seq = await _next_channel_seq(message.channel_id)

            # Build response payload
            msg_payload = {
                "id": message.id,
                "channel_id": message.channel_id,
                "seq": seq,
                "sender": {
                    "id": message.sender.id,
                    "username": message.sender.username,
                    "display_name": message.sender.display_name,
                    "avatar_url": message.sender.avatar_url,
                } if message.sender else None,
                "content": message.content,
                "type": message.type,
                "reply_to": message.reply_to,
                "file_id": message.file_id,
                "status": "sent",
                "reactions": [],
                "edited_at": None,
                "mentions": mentioned_user_ids,
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }

            # Room-based broadcast — O(1) fanout to local subscribers.
            # Channel rooms are populated lazily on first broadcast.
            import asyncio as _asyncio
            from app.socket import channel_room as _channel_room
            await _channel_room.ensure_populated(sio, channel_id)
            room_name = _channel_room.room_name(channel_id)
            try:
                await sio.emit(
                    "v2_chat:new_message",
                    msg_payload,
                    room=room_name,
                    skip_sid=sid,
                )
            except Exception as _e:
                logger.warning(
                    "v2_chat_room_emit_failed",
                    channel_id=channel_id,
                    error=str(_e),
                )

            _failed_members: list[str] = []
            # online_member_sids is kept for the downstream delivered-tick
            # code below — derived from presence, not a DB roundtrip per member.
            online_member_sids: list[tuple[str, str]] = []
            # We still need the authoritative member set for mark_delivered,
            # but load it as a projection (user_id only) — far cheaper than
            # ChannelService.get_channel which pulls the full ORM graph.
            from sqlalchemy import select as _sel_cm
            from app.models.channel import ChannelMember as _CM_
            _member_rows = (await db.execute(
                _sel_cm(_CM_.user_id).where(
                    _CM_.channel_id == channel_id,
                    _CM_.user_id != user_id,
                )
            )).all()
            # Cross-server fan-out: for any member without local sids,
            # forward via fabric (envelope + tracing + idempotency +
            # ACK + DLQ). Members WITH local sids already received via
            # the room=room_name emit above. This makes v2 chat actually
            # multi-server.
            #
            # Canary: when HELEN_FABRIC_EVENT_ALLOWLIST contains
            # "v2_chat:new_message" or matches a wildcard, the emit
            # rides the new fabric. Otherwise it falls through to the
            # legacy emit_to_user (zero behavior change). The
            # message.id + recipient user_id pair gives idempotency
            # keys that survive client retries on transient outages.
            from app.services import fabric_emit as _fe
            for (_mid,) in _member_rows:
                _local_sids = list(presence_service.get_sids(_mid))
                if _local_sids:
                    for _m_sid in _local_sids:
                        online_member_sids.append((_mid, _m_sid))
                else:
                    # No local sid — try fabric (or legacy fallback).
                    # Fire-and-forget so the hot path stays responsive;
                    # the ack_manager (or legacy retry policy) handles
                    # eventual delivery.
                    _asyncio.create_task(_fe.emit_event(
                        event_type="v2_chat:new_message",
                        priority="P2",
                        payload=msg_payload,
                        destination_user_id=_mid,
                        source_user_id=user_id,
                        channel_id=channel_id,
                        idempotency_key=f"chat_msg:{message.id}:{_mid}",
                        sequence=seq,
                    ))

            if _failed_members:
                try:
                    from app.services.dead_letter_service import record as _dlq_record
                    await _dlq_record(
                        kind="fanout",
                        reason="v2_chat_new_message_partial_failure",
                        error=f"failed_member_count={len(_failed_members)}",
                        payload={
                            "event": "v2_chat:new_message",
                            "channel_id": channel_id,
                            "message": msg_payload,
                            "member_ids": _failed_members,
                        },
                        message_id=message.id,
                        channel_id=channel_id,
                        sender_id=user_id,
                    )
                except Exception:
                    pass

            # ── Push real-time notification:new to mentioned users ──
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
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                }
                from app.services import fabric_emit as _fe_notif
                for mentioned_uid in mentioned_user_ids:
                    await _fe_notif.emit_event(
                        event_type="notification:new",
                        priority="P2",
                        payload=mention_payload,
                        destination_user_id=mentioned_uid,
                        source_user_id=user_id,
                        channel_id=channel_id,
                        idempotency_key=f"mention_notif:{message.id}:{mentioned_uid}",
                    )

            # Mark delivered for online recipients in ONE bulk UPDATE —
            # per-recipient mark_delivered calls were 2N SQL roundtrips
            # (SELECT + UPDATE per user) and held the SQLite writer lock
            # long enough to starve concurrent events under big channels.
            online_user_ids = list(set(uid for uid, _ in online_member_sids))
            if online_user_ids:
                from sqlalchemy import update as _sql_update, and_ as _sql_and
                from app.models.message_status import MessageReceipt as _MR
                await db.execute(
                    _sql_update(_MR)
                    .where(_sql_and(
                        _MR.message_id == message.id,
                        _MR.recipient_id.in_(online_user_ids),
                        _MR.delivered_at.is_(None),
                    ))
                    .values(delivered_at=datetime.now(timezone.utc))
                )
                await db.commit()

                # Sender's delivered-to notification is fire-and-forget —
                # sender already has the ack; delivery-tick is a nicety.
                delivered_payload = {
                    "message_id": message.id,
                    "channel_id": channel_id,
                    "delivered_to": online_user_ids,
                    "delivered_at": datetime.now(timezone.utc).isoformat(),
                }
                sender_sids = list(presence_service.get_sids(user_id))
                if sender_sids:
                    await _asyncio.gather(
                        *(sio.emit("v2_chat:message_delivered", delivered_payload, to=s) for s in sender_sids),
                        return_exceptions=True,
                    )

        return {
            "message_id": message.id,
            "client_id": client_id,
            "created_at": message.created_at.isoformat() if message.created_at else None,
        }

    except Exception as e:
        logger.error("v2_chat_send_error", user_id=user_id, error=str(e))
        try:
            from app.services.dead_letter_service import record as _dlq_record
            await _dlq_record(
                kind="fanout",
                reason="v2_chat_send_message_exception",
                error=e,
                payload={
                    "event": "v2_chat:new_message",
                    "channel_id": channel_id,
                    "content": (content or "")[:2048],
                    "type": msg_type,
                    "reply_to": reply_to,
                    "file_id": file_id,
                    "client_id": client_id,
                },
                channel_id=channel_id,
                sender_id=user_id,
            )
        except Exception:
            pass
        return {"error": str(e)}


@sio.event
async def v2_chat_mark_delivered(sid: str, data: dict):
    """
    Mark specific messages as delivered.
    data: { message_ids: [str] }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    message_ids = data.get("message_ids", [])
    if not message_ids or not isinstance(message_ids, list):
        return
    # Limit batch size to prevent abuse
    if len(message_ids) > 500:
        message_ids = message_ids[:500]
    # Validate all IDs are strings
    message_ids = [mid for mid in message_ids if isinstance(mid, str)]
    if not message_ids:
        return

    try:
        async with async_session_factory() as db:
            count = await sync_service.bulk_mark_delivered(
                db, user_id, message_ids=message_ids
            )
            await db.commit()

        # Notify senders
        if count > 0:
            logger.info("v2_delivery_ack", user_id=user_id, message_count=count, total_requested=len(message_ids))
            await _notify_delivered_by_ids(message_ids, user_id)

    except Exception as e:
        logger.error("v2_mark_delivered_error", user_id=user_id, error=str(e), message_count=len(message_ids))


@sio.event
async def v2_chat_mark_read(sid: str, data: dict):
    """
    Mark all messages in a channel as read.
    data: { channel_id: str, up_to_message_id?: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    channel_id = data.get("channel_id")
    up_to = data.get("up_to_message_id")

    if not channel_id or not isinstance(channel_id, str):
        return

    try:
        async with async_session_factory() as db:
            # Validate membership before marking read
            is_member = await ChannelService.is_member(db, channel_id, user_id)
            if not is_member:
                return

            count = await sync_service.mark_read(
                db, channel_id, user_id, up_to
            )
            await db.commit()

        if count > 0:
            # Notify other channel members that this user has read messages
            # Cross-server fan-out via fabric_emit. Read receipts are
            # P2 (delivery-tracked) — idempotency on (reader, channel,
            # up_to_message_id) so retries with same up_to don't
            # re-emit a redundant receipt.
            from app.services import fabric_emit as _fe
            async with async_session_factory() as db:
                ch = await ChannelService.get_channel(db, channel_id)
                read_at_iso = datetime.now(timezone.utc).isoformat()
                for member in ch.members:
                    if member.user_id != user_id:
                        await _fe.emit_event(
                            event_type="v2_chat:message_read",
                            priority="P2",
                            payload={
                                "channel_id": channel_id,
                                "reader_id": user_id,
                                "up_to_message_id": up_to,
                                "read_at": read_at_iso,
                            },
                            destination_user_id=member.user_id,
                            source_user_id=user_id,
                            channel_id=channel_id,
                            idempotency_key=f"read:{channel_id}:{user_id}:{up_to}",
                        )

    except Exception as e:
        logger.error("v2_mark_read_error", user_id=user_id, error=str(e))


@sio.event
async def v2_chat_edit_message(sid: str, data: dict):
    """
    Edit a message and broadcast the update.
    data: { message_id: str, content: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    message_id = data.get("message_id")
    content = data.get("content", "").strip()

    if not message_id or not content:
        return {"error": "message_id and content are required"}

    try:
        async with async_session_factory() as db:
            message = await MessageService.edit_message(db, message_id, user_id, content)
            await db.commit()

            # Broadcast edit to channel members
            ch = await ChannelService.get_channel(db, message.channel_id)
            edit_payload = {
                "message_id": message.id,
                "channel_id": message.channel_id,
                "content": message.content,
                "edited_at": message.edited_at.isoformat() if message.edited_at else None,
                "editor_id": user_id,
            }

            from app.socket.server import emit_to_user as _emit_to_user
            for member in ch.members:
                if member.user_id != user_id:
                    await _emit_to_user("v2_chat:message_edited", edit_payload, member.user_id)

        return {"status": "edited", "edited_at": edit_payload.get("edited_at")}

    except Exception as e:
        logger.error("v2_edit_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def v2_chat_delete_message(sid: str, data: dict):
    """
    Soft-delete a message and broadcast.
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
            message = await MessageService.delete_message(db, message_id, user_id)
            channel_id = message.channel_id
            await db.commit()

            # Broadcast deletion
            ch = await ChannelService.get_channel(db, channel_id)
            delete_payload = {
                "message_id": message_id,
                "channel_id": channel_id,
                "deleted_by": user_id,
            }

            from app.socket.server import emit_to_user as _emit_to_user
            for member in ch.members:
                if member.user_id != user_id:
                    await _emit_to_user("v2_chat:message_deleted", delete_payload, member.user_id)

        return {"status": "deleted"}

    except Exception as e:
        logger.error("v2_delete_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def v2_chat_typing_start(sid: str, data: dict):
    """
    Typing indicator with user info.
    data: { channel_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    channel_id = data.get("channel_id")
    if not channel_id:
        return

    # SECURITY: Verify channel membership before broadcasting typing
    if not await _verify_channel_membership(channel_id, user_id, "v2_typing_start"):
        return

    # Rate limiting: max 3 typing events per 2 seconds per user per channel
    key = (user_id, channel_id)
    now = datetime.now()
    cutoff = now - timedelta(seconds=2)

    if key not in _v2_typing_rate_limit:
        _v2_typing_rate_limit[key] = []

    _v2_typing_rate_limit[key] = [ts for ts in _v2_typing_rate_limit[key] if ts > cutoff]

    if len(_v2_typing_rate_limit[key]) >= 3:
        logger.warning("v2_typing_rate_limit_exceeded", user_id=user_id, channel_id=channel_id)
        return

    _v2_typing_rate_limit[key].append(now)

    try:
        from app.socket.server import emit_to_user as _emit_to_user
        async with async_session_factory() as db:
            ch = await ChannelService.get_channel(db, channel_id)
            for member in ch.members:
                if member.user_id != user_id:
                    await _emit_to_user("v2_chat:typing", {
                        "channel_id": channel_id,
                        "user_id": user_id,
                        "is_typing": True,
                    }, member.user_id)
    except Exception as e:
        logger.error("v2_typing_start_error", error=str(e))


@sio.event
async def v2_chat_typing_stop(sid: str, data: dict):
    """
    Typing indicator stop.
    data: { channel_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    channel_id = data.get("channel_id")
    if not channel_id:
        return

    # SECURITY: Verify channel membership
    if not await _verify_channel_membership(channel_id, user_id, "v2_typing_stop"):
        return

    # Rate limiting: max 3 typing events per 2 seconds per user per channel
    key = (user_id, channel_id)
    now = datetime.now()
    cutoff = now - timedelta(seconds=2)

    if key not in _v2_typing_rate_limit:
        _v2_typing_rate_limit[key] = []

    _v2_typing_rate_limit[key] = [ts for ts in _v2_typing_rate_limit[key] if ts > cutoff]

    if len(_v2_typing_rate_limit[key]) >= 3:
        logger.warning("v2_typing_rate_limit_exceeded", user_id=user_id, channel_id=channel_id)
        return

    _v2_typing_rate_limit[key].append(now)

    try:
        from app.socket.server import emit_to_user as _emit_to_user
        async with async_session_factory() as db:
            ch = await ChannelService.get_channel(db, channel_id)
            for member in ch.members:
                if member.user_id != user_id:
                    await _emit_to_user("v2_chat:typing", {
                        "channel_id": channel_id,
                        "user_id": user_id,
                        "is_typing": False,
                    }, member.user_id)
    except Exception as e:
        logger.error("v2_typing_stop_error", error=str(e))


@sio.event
async def v2_chat_reaction(sid: str, data: dict):
    """
    Toggle reaction on a message.
    data: { message_id: str, emoji: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    message_id = data.get("message_id")
    emoji = data.get("emoji")

    if not message_id or not emoji or not isinstance(emoji, str):
        return
    # Validate emoji length (most emoji are 1-4 codepoints)
    if len(emoji) > 8:
        return

    try:
        async with async_session_factory() as db:
            reactions = await MessageService.toggle_reaction(db, message_id, user_id, emoji)
            aggregated = MessageService.aggregate_reactions(reactions)
            await db.commit()

            # Get message to find channel
            from sqlalchemy import select
            from app.models.message import Message
            stmt = select(Message.channel_id).where(Message.id == message_id)
            result = await db.execute(stmt)
            channel_id = result.scalar_one_or_none()

            if channel_id:
                ch = await ChannelService.get_channel(db, channel_id)
                reaction_payload = {
                    "message_id": message_id,
                    "channel_id": channel_id,
                    "reactions": aggregated,
                    "toggled_by": user_id,
                    "emoji": emoji,
                }
                from app.socket.server import emit_to_user as _emit_to_user
                for member in ch.members:
                    await _emit_to_user("v2_chat:reaction_update", reaction_payload, member.user_id)

    except Exception as e:
        logger.error("v2_reaction_error", user_id=user_id, error=str(e))


# ══════════════════════════════════════════════════════
# ── Helper Functions ─────────────────────────────────
# ══════════════════════════════════════════════════════


async def _notify_delivered_bulk(
    message_ids: list[str],
    delivered_to: str,
    missed_grouped: dict[str, list[dict]],
) -> None:
    """Notify message senders that their messages were delivered during sync."""
    # Group by sender
    sender_messages: dict[str, list[str]] = {}
    for msgs in missed_grouped.values():
        for msg in msgs:
            sender_id = msg.get("sender", {}).get("id")
            if sender_id and sender_id != delivered_to:
                if sender_id not in sender_messages:
                    sender_messages[sender_id] = []
                sender_messages[sender_id].append(msg["id"])

    from app.services import fabric_emit as _fe
    now = datetime.now(timezone.utc).isoformat()
    for sender_id, msg_ids in sender_messages.items():
        # Idempotency on (delivered_to, sorted msg_ids) so a retry
        # with the same set returns the cached result.
        ids_key = ",".join(sorted(msg_ids))[:64]
        await _fe.emit_event(
            event_type="v2_chat:message_delivered",
            priority="P2",
            payload={
                "message_ids": msg_ids,
                "delivered_to": delivered_to,
                "delivered_at": now,
            },
            destination_user_id=sender_id,
            source_user_id=delivered_to,
            idempotency_key=f"delivered:{delivered_to}:{ids_key}",
        )


async def _notify_delivered_by_ids(
    message_ids: list[str],
    delivered_to: str,
) -> None:
    """Notify senders of specific messages that they were delivered."""
    # We need to look up sender IDs from the database
    try:
        from sqlalchemy import select
        from app.models.message import Message

        async with async_session_factory() as db:
            stmt = select(Message.id, Message.sender_id).where(
                Message.id.in_(message_ids)
            )
            result = await db.execute(stmt)
            rows = result.all()

        sender_messages: dict[str, list[str]] = {}
        for msg_id, sender_id in rows:
            if sender_id != delivered_to:
                if sender_id not in sender_messages:
                    sender_messages[sender_id] = []
                sender_messages[sender_id].append(msg_id)

        from app.services import fabric_emit as _fe
        now = datetime.now(timezone.utc).isoformat()
        for sender_id, msg_ids in sender_messages.items():
            ids_key = ",".join(sorted(msg_ids))[:64]
            await _fe.emit_event(
                event_type="v2_chat:message_delivered",
                priority="P2",
                payload={
                    "message_ids": msg_ids,
                    "delivered_to": delivered_to,
                    "delivered_at": now,
                },
                destination_user_id=sender_id,
                source_user_id=delivered_to,
                idempotency_key=f"delivered:{delivered_to}:{ids_key}",
            )

    except Exception as e:
        logger.error("notify_delivered_error", error=str(e))


# ══════════════════════════════════════════════════════
# ── V2 Chat — Threads, Pins, Receipts, Read States ──
# ══════════════════════════════════════════════════════


@sio.event
async def v2_chat_get_thread(sid: str, data: dict):
    """
    Fetch all replies to a parent message.
    data: { message_id: str, limit?: int, before?: ISO timestamp }
    Returns: { thread: [message_dicts], parent_id }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    message_id = data.get("message_id")
    if not message_id or not isinstance(message_id, str):
        return {"error": "message_id is required"}

    limit = min(int(data.get("limit") or 50), 200)
    before_str = data.get("before")
    before = None
    if before_str:
        try:
            before = datetime.fromisoformat(before_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return {"error": "Invalid 'before' timestamp"}

    try:
        async with async_session_factory() as db:
            messages = await MessageService.get_thread(
                db, message_id, user_id, limit=limit, before=before
            )

            thread = []
            for msg in messages:
                thread.append({
                    "id": msg.id,
                    "channel_id": msg.channel_id,
                    "sender": {
                        "id": msg.sender.id,
                        "username": msg.sender.username,
                        "display_name": msg.sender.display_name,
                        "avatar_url": msg.sender.avatar_url,
                    } if msg.sender else None,
                    "content": msg.content,
                    "type": msg.type,
                    "reply_to": msg.reply_to,
                    "file_id": msg.file_id,
                    "reactions": MessageService.aggregate_reactions(msg.reactions or []),
                    "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                })

        return {"parent_id": message_id, "thread": thread}

    except Exception as e:
        logger.error("v2_get_thread_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def v2_chat_get_pinned_messages(sid: str, data: dict):
    """
    Fetch all pinned messages in a channel.
    data: { channel_id: str }
    Returns: { channel_id, pinned: [message_dicts] }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    channel_id = data.get("channel_id")
    if not channel_id or not isinstance(channel_id, str):
        return {"error": "channel_id is required"}

    try:
        async with async_session_factory() as db:
            messages = await MessageService.get_pinned_messages(db, channel_id, user_id)

            pinned = []
            for msg in messages:
                pinned.append({
                    "id": msg.id,
                    "channel_id": msg.channel_id,
                    "sender": {
                        "id": msg.sender.id,
                        "username": msg.sender.username,
                        "display_name": msg.sender.display_name,
                        "avatar_url": msg.sender.avatar_url,
                    } if msg.sender else None,
                    "content": msg.content,
                    "type": msg.type,
                    "pinned_at": msg.pinned_at.isoformat() if msg.pinned_at else None,
                    "pinned_by": msg.pinned_by,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                })

        return {"channel_id": channel_id, "pinned": pinned}

    except Exception as e:
        logger.error("v2_get_pinned_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def v2_chat_get_message_receipts(sid: str, data: dict):
    """
    Fetch delivery/read receipt summary for a message (sender-only).
    data: { message_id: str }
    Returns: { message_id, delivered_count, read_count, total_recipients }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    message_id = data.get("message_id")
    if not message_id or not isinstance(message_id, str):
        return {"error": "message_id is required"}

    try:
        async with async_session_factory() as db:
            # Verify the requester is the sender (only sender sees receipts)
            from sqlalchemy import select
            from app.models.message import Message
            stmt = select(Message.sender_id, Message.channel_id).where(
                Message.id == message_id
            )
            result = await db.execute(stmt)
            row = result.one_or_none()
            if not row:
                return {"error": "Message not found"}
            sender_id, channel_id = row
            if sender_id != user_id:
                # Channel members can see aggregate but not per-user details
                if not await ChannelService.is_member(db, channel_id, user_id):
                    return {"error": "Not authorized"}

            summary = await sync_service.get_receipt_summary(db, message_id)

        # Return aggregate only (no per-user) when not the sender
        if sender_id != user_id:
            summary = {
                "message_id": summary["message_id"],
                "delivered_count": summary["delivered_count"],
                "read_count": summary["read_count"],
                "total_recipients": summary["total_recipients"],
            }

        return summary

    except Exception as e:
        logger.error("v2_get_receipts_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def v2_chat_get_receipt_details(sid: str, data: dict):
    """
    Fetch per-user receipt details (sender-only).
    data: { message_id: str }
    Returns: full receipt summary including each recipient's status.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    message_id = data.get("message_id")
    if not message_id or not isinstance(message_id, str):
        return {"error": "message_id is required"}

    try:
        async with async_session_factory() as db:
            from sqlalchemy import select
            from app.models.message import Message
            stmt = select(Message.sender_id).where(Message.id == message_id)
            result = await db.execute(stmt)
            sender_id = result.scalar_one_or_none()

            if sender_id is None:
                return {"error": "Message not found"}
            if sender_id != user_id:
                return {"error": "Only the sender can view detailed receipts"}

            summary = await sync_service.get_receipt_summary(db, message_id)

        return summary

    except Exception as e:
        logger.error("v2_get_receipt_details_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def v2_chat_get_channel_read_states(sid: str, data: dict):
    """
    Fetch per-member last_read_at + unread counts for a channel.
    data: { channel_id: str }
    Returns: { channel_id, states: [{user_id, username, last_read_at, unread_count}] }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    channel_id = data.get("channel_id")
    if not channel_id or not isinstance(channel_id, str):
        return {"error": "channel_id is required"}

    try:
        async with async_session_factory() as db:
            if not await ChannelService.is_member(db, channel_id, user_id):
                return {"error": "Not a member of this channel"}

            states = await sync_service.get_channel_read_states(db, channel_id)

        return {"channel_id": channel_id, "states": states}

    except Exception as e:
        logger.error("v2_get_read_states_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def v2_chat_acknowledge_read(sid: str, data: dict):
    """
    Lightweight "I have actually seen these messages" acknowledgement.
    data: { channel_id: str, message_ids: [str], up_to_message_id?: str }
    Updates last_read_at and emits read receipts to senders.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    channel_id = data.get("channel_id")
    if not channel_id or not isinstance(channel_id, str):
        return
    up_to = data.get("up_to_message_id")
    message_ids = data.get("message_ids") or []
    if not isinstance(message_ids, list):
        message_ids = []

    try:
        async with async_session_factory() as db:
            if not await ChannelService.is_member(db, channel_id, user_id):
                return
            count = await sync_service.mark_read(db, channel_id, user_id, up_to)
            await db.commit()

        # Notify senders that messages were read (fabric-aware).
        if count > 0:
            from app.services import fabric_emit as _fe
            async with async_session_factory() as db:
                ch = await ChannelService.get_channel(db, channel_id)
                read_at_iso = datetime.now(timezone.utc).isoformat()
                for member in ch.members:
                    if member.user_id != user_id:
                        await _fe.emit_event(
                            event_type="v2_chat:message_read",
                            priority="P2",
                            payload={
                                "channel_id": channel_id,
                                "reader_id": user_id,
                                "up_to_message_id": up_to,
                                "message_ids": message_ids,
                                "read_at": read_at_iso,
                            },
                            destination_user_id=member.user_id,
                            source_user_id=user_id,
                            channel_id=channel_id,
                            idempotency_key=f"ack_read:{channel_id}:{user_id}:{up_to}",
                        )

        return {"status": "acknowledged", "count": count}

    except Exception as e:
        logger.error("v2_ack_read_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


@sio.event
async def v2_chat_confirm_batch_delivery(sid: str, data: dict):
    """
    Batch confirm delivery for multiple messages (more efficient than individual acks).
    data: { message_ids: [str] }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    message_ids = data.get("message_ids", [])
    if not isinstance(message_ids, list) or not message_ids:
        return {"status": "no_op", "count": 0}
    if len(message_ids) > 1000:
        message_ids = message_ids[:1000]
    message_ids = [mid for mid in message_ids if isinstance(mid, str)]
    if not message_ids:
        return {"status": "no_op", "count": 0}

    try:
        async with async_session_factory() as db:
            count = await sync_service.bulk_mark_delivered(
                db, user_id, message_ids=message_ids
            )
            await db.commit()

        if count > 0:
            await _notify_delivered_by_ids(message_ids, user_id)

        return {"status": "confirmed", "count": count}

    except Exception as e:
        logger.error("v2_batch_delivery_error", user_id=user_id, error=str(e))
        return {"error": str(e)}


# ══════════════════════════════════════════════════════
# ── Universal message:send shortcut ──────────────────
# ══════════════════════════════════════════════════════


@sio.on("message:send")
async def message_send_alias(sid: str, data: dict):
    """
    Lightweight wrapper used by FileDropManager / VoiceRecorder to ship a
    message+attachment via the same path as v2_chat_send_message.
    Accepts: { channel_id, content, type, file_id?, recipient_id? }
    """
    return await v2_chat_send_message(sid, data)
