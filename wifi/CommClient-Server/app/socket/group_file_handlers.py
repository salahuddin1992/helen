"""
Socket.IO handlers for group-file multicast offers.

Protocol
--------
Client → Server
  file_drop:group_offer          — sender announces a new multicast offer
  file_drop:group_accept         — recipient accepts
  file_drop:group_reject         — recipient rejects
  file_drop:group_chunk_received — recipient reports a chunk landed
  file_drop:group_chunk_peers    — recipient asks "who has chunk N?"
  file_drop:group_cancel         — sender cancels an in-flight offer

Server → clients (fan-out)
  file_drop:group_offer_created   — every channel member gets metadata
  file_drop:group_offer_updated   — status / counter change
  file_drop:group_peer_available  — chunk N is now available from user X
  file_drop:group_offer_completed — offer moved to a terminal state
  file_drop:group_offer_ack       — ack to the caller only
  file_drop:group_offer_error     — protocol error to the caller only

Each handler isolates exceptions so a misbehaving client can't kill the
event loop. All mutation paths go through ``GroupFileService`` so state
stays consistent with the DB.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.services.channel_service import ChannelService
from app.services.group_file_service import GroupFileService
from app.services.presence_service import presence_service
from app.socket.server import emit_to_user, get_user_id, sio

logger = get_logger(__name__)


# ── Helpers ────────────────────────────────────────────────────────

async def _ack(sid: str, event: str, payload: dict) -> None:
    try:
        await sio.emit(event, payload, to=sid)
    except Exception as e:  # pragma: no cover
        logger.warning("group_file_ack_emit_failed", event=event, error=str(e))


async def _err(sid: str, reason: str, detail: str | None = None) -> None:
    await _ack(sid, "file_drop:group_offer_error", {
        "reason": reason,
        "detail": detail,
    })


async def _fanout_to_members(
    event: str, payload: dict, member_ids: list[str],
    skip_user_id: str | None = None,
    *,
    channel_id: str | None = None,
    source_user_id: str | None = None,
    idempotency_seed: str | None = None,
) -> None:
    """Fan-out to channel members. Routes through fabric_emit so
    members on sibling Helen servers receive the event with full
    fabric treatment (envelope + tracing + ACK + DLQ when in the
    allowlist; legacy emit_to_user fallback otherwise).

    P4 priority — file metadata is best-effort from the fabric's
    perspective (heavy retry isn't useful; the underlying object
    storage / chunk swarm has its own retry logic)."""
    from app.services import fabric_emit as _fe
    for uid in member_ids:
        if skip_user_id and uid == skip_user_id:
            continue
        try:
            await _fe.emit_event(
                event_type=event,
                priority="P4",
                payload=payload,
                destination_user_id=uid,
                source_user_id=source_user_id,
                channel_id=channel_id,
                idempotency_key=(
                    f"{event}:{idempotency_seed}:{uid}"
                    if idempotency_seed else None
                ),
                requires_ack=False,
            )
        except Exception as e:
            logger.warning("group_file_fanout_error",
                           event=event, user_id=uid, error=str(e))


async def _channel_member_ids(channel_id: str) -> list[str]:
    async with async_session_factory() as db:
        channel = await ChannelService.get_channel(db, channel_id)
        return [m.user_id for m in (channel.members or [])]


# ── file_drop:group_offer ──────────────────────────────────────────

@sio.event
async def file_drop_group_offer(sid: str, data: dict):
    """
    Sender announces a new multicast offer.

    Expected data: {
        channel_id, file_id,
        chunk_size (int, bytes),
        total_chunks (int),
        caption (str | None),
        swarm_enabled (bool, default true),
        expires_in_sec (int | None),
        checksum (str | None),
    }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        await _err(sid, "unauthenticated")
        return
    try:
        channel_id = data.get("channel_id")
        file_id = data.get("file_id")
        chunk_size = int(data.get("chunk_size") or 0)
        total_chunks = int(data.get("total_chunks") or 0)
        caption = data.get("caption")
        swarm_enabled = bool(data.get("swarm_enabled", True))
        exp_sec = data.get("expires_in_sec")
        checksum = data.get("checksum")

        if not channel_id or not file_id:
            await _err(sid, "missing_fields",
                       "channel_id and file_id are required")
            return

        expires_in = timedelta(seconds=int(exp_sec)) if exp_sec else None

        async with async_session_factory() as db:
            offer = await GroupFileService.create_offer(
                db,
                sender_id=user_id,
                channel_id=channel_id,
                file_id=file_id,
                chunk_size=chunk_size,
                total_chunks=total_chunks,
                caption=caption,
                swarm_enabled=swarm_enabled,
                expires_in=expires_in,
                checksum=checksum,
            )
            payload = offer.to_dict()

        member_ids = await _channel_member_ids(channel_id)
        await _fanout_to_members(
            "file_drop:group_offer_created", payload, member_ids,
        )
        await _ack(sid, "file_drop:group_offer_ack", {
            "ok": True, "offer": payload,
        })
        logger.info("group_file_offer_socket_created",
                    offer_id=offer.id, channel_id=channel_id)

    except AppError as e:
        await _err(sid, e.__class__.__name__.lower(), str(e))
    except ValueError as e:
        await _err(sid, "validation_error", str(e))
    except Exception as e:
        logger.error("group_file_offer_handler_error", error=str(e))
        await _err(sid, "internal_error", "unexpected error")


# ── file_drop:group_accept ─────────────────────────────────────────

@sio.event
async def file_drop_group_accept(sid: str, data: dict):
    user_id = await get_user_id(sid)
    if not user_id:
        await _err(sid, "unauthenticated")
        return
    try:
        offer_id = data.get("offer_id")
        if not offer_id:
            await _err(sid, "missing_fields", "offer_id is required")
            return
        async with async_session_factory() as db:
            offer, row = await GroupFileService.accept_offer(
                db, offer_id, user_id,
            )
            payload = {
                "offer": offer.to_dict(),
                "availability": row.to_dict(total_chunks=offer.total_chunks),
            }
        await _ack(sid, "file_drop:group_offer_ack",
                   {"ok": True, "action": "accept", **payload})

        # Notify sender + channel that counters changed.
        member_ids = await _channel_member_ids(offer.channel_id)
        await _fanout_to_members(
            "file_drop:group_offer_updated",
            {"offer_id": offer.id, "offer": offer.to_dict(),
             "event": "accepted", "user_id": user_id},
            member_ids,
        )
    except AppError as e:
        await _err(sid, e.__class__.__name__.lower(), str(e))
    except Exception as e:
        logger.error("group_file_accept_handler_error", error=str(e))
        await _err(sid, "internal_error", "unexpected error")


# ── file_drop:group_reject ─────────────────────────────────────────

@sio.event
async def file_drop_group_reject(sid: str, data: dict):
    user_id = await get_user_id(sid)
    if not user_id:
        await _err(sid, "unauthenticated")
        return
    try:
        offer_id = data.get("offer_id")
        if not offer_id:
            await _err(sid, "missing_fields", "offer_id is required")
            return
        async with async_session_factory() as db:
            offer, row = await GroupFileService.reject_offer(
                db, offer_id, user_id,
            )
        await _ack(sid, "file_drop:group_offer_ack",
                   {"ok": True, "action": "reject",
                    "offer_id": offer.id})

        member_ids = await _channel_member_ids(offer.channel_id)
        await _fanout_to_members(
            "file_drop:group_offer_updated",
            {"offer_id": offer.id, "offer": offer.to_dict(),
             "event": "rejected", "user_id": user_id},
            member_ids,
        )
        if offer.is_terminal():
            await _fanout_to_members(
                "file_drop:group_offer_completed",
                {"offer_id": offer.id, "status": offer.status},
                member_ids,
            )
    except AppError as e:
        await _err(sid, e.__class__.__name__.lower(), str(e))
    except Exception as e:
        logger.error("group_file_reject_handler_error", error=str(e))
        await _err(sid, "internal_error", "unexpected error")


# ── file_drop:group_chunk_received ─────────────────────────────────

@sio.event
async def file_drop_group_chunk_received(sid: str, data: dict):
    """
    Expected data: { offer_id, chunk_index, chunk_bytes? }
    Broadcasts a ``group_peer_available`` on the first time this peer
    reports this chunk so other leechers can pull it swarm-style.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        await _err(sid, "unauthenticated")
        return
    try:
        offer_id = data.get("offer_id")
        chunk_index = data.get("chunk_index")
        chunk_bytes = data.get("chunk_bytes")
        if offer_id is None or chunk_index is None:
            await _err(sid, "missing_fields",
                       "offer_id and chunk_index are required")
            return
        chunk_index = int(chunk_index)

        async with async_session_factory() as db:
            row, flipped, became_complete = (
                await GroupFileService.report_chunk_received(
                    db, offer_id, user_id, chunk_index,
                    chunk_bytes=chunk_bytes,
                )
            )
            offer = await GroupFileService.get_offer(db, offer_id)
            channel_id = offer.channel_id
            total_chunks = offer.total_chunks

        if flipped and offer.swarm_enabled:
            member_ids = await _channel_member_ids(channel_id)
            await _fanout_to_members(
                "file_drop:group_peer_available",
                {
                    "offer_id": offer_id,
                    "chunk_index": chunk_index,
                    "user_id": user_id,
                    "chunks_received": row.chunks_received,
                    "total_chunks": total_chunks,
                },
                member_ids,
                skip_user_id=user_id,
            )

        if became_complete:
            member_ids = await _channel_member_ids(channel_id)
            await _fanout_to_members(
                "file_drop:group_offer_updated",
                {"offer_id": offer_id, "offer": offer.to_dict(),
                 "event": "recipient_completed", "user_id": user_id},
                member_ids,
            )
            if offer.is_terminal():
                await _fanout_to_members(
                    "file_drop:group_offer_completed",
                    {"offer_id": offer_id, "status": offer.status},
                    member_ids,
                )

    except AppError as e:
        await _err(sid, e.__class__.__name__.lower(), str(e))
    except Exception as e:
        logger.error("group_file_chunk_received_handler_error",
                     error=str(e))
        await _err(sid, "internal_error", "unexpected error")


# ── file_drop:group_chunk_peers ────────────────────────────────────

@sio.event
async def file_drop_group_chunk_peers(sid: str, data: dict):
    """
    Swarm lookup. Expected data: { offer_id, chunk_index }.
    Responds on ``file_drop:group_chunk_peers_result`` with a peer list.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        await _err(sid, "unauthenticated")
        return
    try:
        offer_id = data.get("offer_id")
        chunk_index = data.get("chunk_index")
        if offer_id is None or chunk_index is None:
            await _err(sid, "missing_fields",
                       "offer_id and chunk_index are required")
            return
        chunk_index = int(chunk_index)
        async with async_session_factory() as db:
            peers = await GroupFileService.get_chunk_peers(
                db, offer_id, chunk_index, exclude_user_id=user_id,
            )
        await _ack(sid, "file_drop:group_chunk_peers_result", {
            "offer_id": offer_id,
            "chunk_index": chunk_index,
            "peers": peers,
        })
    except AppError as e:
        await _err(sid, e.__class__.__name__.lower(), str(e))
    except Exception as e:
        logger.error("group_file_chunk_peers_handler_error", error=str(e))
        await _err(sid, "internal_error", "unexpected error")


# ── file_drop:group_cancel ─────────────────────────────────────────

@sio.event
async def file_drop_group_cancel(sid: str, data: dict):
    user_id = await get_user_id(sid)
    if not user_id:
        await _err(sid, "unauthenticated")
        return
    try:
        offer_id = data.get("offer_id")
        if not offer_id:
            await _err(sid, "missing_fields", "offer_id is required")
            return
        async with async_session_factory() as db:
            offer = await GroupFileService.cancel_offer(
                db, offer_id, user_id,
            )
        member_ids = await _channel_member_ids(offer.channel_id)
        await _fanout_to_members(
            "file_drop:group_offer_completed",
            {"offer_id": offer.id, "status": offer.status,
             "reason": "cancelled", "by": user_id},
            member_ids,
        )
        await _ack(sid, "file_drop:group_offer_ack",
                   {"ok": True, "action": "cancel",
                    "offer_id": offer.id})
    except AppError as e:
        await _err(sid, e.__class__.__name__.lower(), str(e))
    except Exception as e:
        logger.error("group_file_cancel_handler_error", error=str(e))
        await _err(sid, "internal_error", "unexpected error")


__all__ = [
    "file_drop_group_offer",
    "file_drop_group_accept",
    "file_drop_group_reject",
    "file_drop_group_chunk_received",
    "file_drop_group_chunk_peers",
    "file_drop_group_cancel",
]
