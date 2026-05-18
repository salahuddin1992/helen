"""
Message REST endpoints — history, search, edit, delete, reactions.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.schemas.message import (
    MessageCreate,
    MessageListResponse,
    MessageResponse,
    MessageSearchResponse,
    MessageUpdate,
    ReactionCreate,
    SenderBrief,
)
from app.services.message_service import MessageService

router = APIRouter(prefix="/channels/{channel_id}/messages", tags=["messages"])


def _msg_to_response(msg) -> MessageResponse:
    return MessageResponse(
        id=msg.id,
        channel_id=msg.channel_id,
        sender=SenderBrief(
            id=msg.sender.id,
            username=msg.sender.username,
            display_name=msg.sender.display_name,
            avatar_url=msg.sender.avatar_url,
        ),
        content=msg.content,
        type=msg.type,
        reply_to=msg.reply_to,
        file_id=msg.file_id,
        status=msg.status,
        reactions=MessageService.aggregate_reactions(msg.reactions),
        edited_at=msg.edited_at,
        created_at=msg.created_at,
    )


@router.get("", response_model=MessageListResponse)
async def get_messages(
    channel_id: str,
    before: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    messages, has_more, total = await MessageService.get_messages(
        db, channel_id, user_id, before=before, limit=limit,
    )
    return MessageListResponse(
        messages=[_msg_to_response(m) for m in messages],
        has_more=has_more,
        total=total,
    )


@router.post("", response_model=MessageResponse, status_code=201)
async def send_message(
    channel_id: str,
    body: MessageCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    msg = await MessageService.send_message(
        db,
        channel_id=channel_id,
        sender_id=user_id,
        content=body.content,
        msg_type=body.type,
        reply_to=body.reply_to,
        file_id=body.file_id,
    )

    # ── @mention parsing + notification + push + socket fanout ──
    try:
        sender_username = msg.sender.username if msg.sender else None
        mentioned = await MessageService.dispatch_mentions(
            db, msg, sender_username=sender_username
        )
        await db.commit()
        if mentioned:
            # Best-effort real-time fanout to mentioned users
            try:
                from app.socket.server import sio
                from app.services.presence_service import presence_service

                payload = {
                    "type": "mention",
                    "title": (
                        f"@{sender_username} mentioned you"
                        if sender_username else "You were mentioned"
                    ),
                    "body": (msg.content or "")[:280],
                    "reference_id": msg.id,
                    "reference_type": "message",
                    "channel_id": channel_id,
                    "message_id": msg.id,
                    "sender_id": user_id,
                    "sender_username": sender_username,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                }
                for uid in mentioned:
                    for s in presence_service.get_sids(uid) or []:
                        try:
                            await sio.emit("notification:new", payload, to=s)
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        # Never let mention handling break a send
        pass

    return _msg_to_response(msg)


# ── Search (global or per-channel) ─────────────────────

search_router = APIRouter(prefix="/messages/search", tags=["messages"])


@search_router.get("", response_model=MessageSearchResponse)
async def search_messages(
    q: str | None = Query(None, max_length=500),
    channel_id: str | None = Query(None),
    sender_id: str | None = Query(None),
    sender_username: str | None = Query(None),
    msg_type: str | None = Query(None, pattern=r"^(text|file|image|reply|system)$"),
    has_file: bool | None = Query(None),
    has_reactions: bool | None = Query(None),
    is_pinned: bool | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Advanced message search.
    All filters are optional; combine to narrow results. At least one of
    `q`, sender, channel, type, has_file, has_reactions, is_pinned, date range
    should be provided to avoid scanning the entire user inbox.
    """
    messages, total = await MessageService.search_messages(
        db, user_id,
        query_text=q,
        channel_id=channel_id,
        sender_id=sender_id,
        sender_username=sender_username,
        msg_type=msg_type,
        has_file=has_file,
        has_reactions=has_reactions,
        is_pinned=is_pinned,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return MessageSearchResponse(
        messages=[_msg_to_response(m) for m in messages],
        total=total,
        query=q or "",
    )


# ── Message-level actions ───────────────────────────────

msg_router = APIRouter(prefix="/messages", tags=["messages"])


@msg_router.patch("/{message_id}", response_model=MessageResponse)
async def edit_message(
    message_id: str,
    body: MessageUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    msg = await MessageService.edit_message(db, message_id, user_id, body.content)
    return _msg_to_response(msg)


@msg_router.get("/{message_id}/history")
async def get_edit_history(
    message_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the chronological edit history for a message. Each entry holds the
    content the message had BEFORE that edit, plus when and by whom it was
    edited. Channel members only.
    """
    history = await MessageService.get_edit_history(db, message_id, user_id)
    return {
        "message_id": message_id,
        "history": [
            {
                "id": h.id,
                "previous_content": h.previous_content,
                "edited_at": h.edited_at,
                "editor_id": h.editor_id,
            }
            for h in history
        ],
        "total": len(history),
    }


@msg_router.delete("/{message_id}", status_code=204, response_class=Response)
async def delete_message(
    message_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await MessageService.delete_message(db, message_id, user_id)
    return Response(status_code=204)


@msg_router.post("/{message_id}/reactions")
async def toggle_reaction(
    message_id: str,
    body: ReactionCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    reactions = await MessageService.toggle_reaction(db, message_id, user_id, body.emoji)
    return {"reactions": MessageService.aggregate_reactions(reactions)}


@msg_router.post("/{message_id}/read")
async def mark_read(
    message_id: str,
    channel_id: str = Query(...),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Mark messages in a channel as read up to ``message_id``.

    Updates BOTH:
      * ``ChannelMember.last_read_at`` (coarse-grained — used for channel
        unread counts).
      * Per-recipient ``MessageReceipt.read_at`` rows (fine-grained — used
        by the UI to show per-user read state).
    """
    from app.services.sync_service import sync_service as _sync
    # Coarse-grained (channel membership) — legacy / compatibility.
    await MessageService.mark_read(db, channel_id, user_id, message_id)
    # Fine-grained per-recipient receipts — this is what powers
    # per-user read indicators in group chats.
    count = await _sync.mark_read(db, channel_id, user_id, message_id)
    await db.commit()
    return {"status": "read", "updated_receipts": count}


# ── V2 Message Endpoints ────────────────────────────────────


@msg_router.get("/{message_id}/receipts")
async def get_message_receipts(
    message_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch delivery/read receipts for a message.
    Returns delivered and read counts per user.
    """
    from app.services.sync_service import sync_service
    receipt_summary = await sync_service.get_receipt_summary(db, message_id)
    return receipt_summary


channel_router = APIRouter(prefix="/channels", tags=["messages"])


@channel_router.get("/{channel_id}/unread")
async def get_channel_unread(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get unread message count for a specific channel.
    """
    from app.services.sync_service import sync_service
    unread_counts = await sync_service.get_unread_counts(db, user_id)
    channel_unread = unread_counts.get(channel_id, 0)
    return {"channel_id": channel_id, "unread_count": channel_unread}


@channel_router.get("/{channel_id}/read-states")
async def get_channel_read_states(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get who has read what in the channel.
    Returns per-message read state for all channel members.
    """
    from app.services.sync_service import sync_service
    read_states = await sync_service.get_channel_read_states(db, channel_id)
    return {"channel_id": channel_id, "read_states": read_states}


@msg_router.post("/{message_id}/pin")
async def pin_message(
    message_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Pin a message in its channel.
    """
    await MessageService.pin_message(db, message_id, user_id)
    return {"message_id": message_id, "status": "pinned"}


@msg_router.delete("/{message_id}/pin")
async def unpin_message(
    message_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Unpin a message from its channel.
    """
    await MessageService.unpin_message(db, message_id, user_id)
    return {"message_id": message_id, "status": "unpinned"}


@channel_router.get("/{channel_id}/pins")
async def get_pinned_messages(
    channel_id: str,
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all pinned messages in a channel.
    """
    pinned = await MessageService.get_pinned_messages(db, channel_id, user_id)
    return {
        "channel_id": channel_id,
        "pinned_messages": [_msg_to_response(m) for m in pinned],
        "count": len(pinned),
    }


@msg_router.post("/{message_id}/forward")
async def forward_message(
    message_id: str,
    body: dict,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Forward a message to another channel.
    body: { target_channel_id: str, content: str (optional prepend) }
    """
    target_channel_id = body.get("target_channel_id")
    content_prepend = body.get("content", "").strip()

    if not target_channel_id:
        return {"error": "target_channel_id is required"}

    forwarded = await MessageService.forward_message(
        db, message_id, target_channel_id, user_id
    )
    return {
        "original_message_id": message_id,
        "forwarded_message": _msg_to_response(forwarded),
    }


@msg_router.get("/{message_id}/thread")
async def get_message_thread(
    message_id: str,
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get thread replies for a message (if it's a parent message or has replies).
    """
    thread = await MessageService.get_thread(db, message_id, user_id, limit=limit)
    return {
        "message_id": message_id,
        "thread": [_msg_to_response(m) for m in thread],
        "count": len(thread),
    }
