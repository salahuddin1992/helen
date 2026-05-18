"""
Socket.IO handlers for file drop — real-time progress, offers, and notifications.

Events:
  file_drop:transfer_progress — Broadcast transfer progress to participants
  file_drop:transfer_complete — Notify completion
  file_drop:transfer_failed — Notify failure
  file_drop:offer — Offer file to recipient(s)
  file_drop:accept — Accept file offer
  file_drop:reject — Reject file offer
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.services.presence_service import presence_service
from app.socket.server import emit_to_user, get_user_id, sio

if TYPE_CHECKING:
    from app.models.file_drop import FileTransfer

logger = get_logger(__name__)


@sio.event
async def file_drop_transfer_progress(sid: str, data: dict):
    """
    Broadcast transfer progress to all participants.
    Expected data: { transfer_id, received_chunks, total_chunks, progress_percent, speed_bps }
    """
    try:
        transfer_id = data.get("transfer_id")
        channel_id = data.get("channel_id")
        receiver_id = data.get("receiver_id")

        payload = {
            "transfer_id": transfer_id,
            "received_chunks": data.get("received_chunks"),
            "total_chunks": data.get("total_chunks"),
            "progress_percent": data.get("progress_percent", 0),
            "speed_bps": data.get("speed_bps"),
            "timestamp": data.get("timestamp"),
        }

        # Broadcast to channel if group transfer
        if channel_id:
            await sio.emit(
                "file_drop:transfer_progress",
                payload,
                to=channel_id,
                skip_sid=sid,
            )
        # Notify receiver if DM
        elif receiver_id:
            await sio.emit(
                "file_drop:transfer_progress",
                payload,
                to=receiver_id,
            )

        logger.debug(
            "file_transfer_progress_broadcast",
            transfer_id=transfer_id,
            progress=data.get("progress_percent"),
        )

    except Exception as e:
        logger.error("file_drop_progress_error", error=str(e))


@sio.event
async def file_drop_transfer_complete(sid: str, data: dict):
    """
    Notify that file transfer completed successfully.
    Expected data: { transfer_id, filename, file_size, channel_id|receiver_id }
    """
    try:
        transfer_id = data.get("transfer_id")
        channel_id = data.get("channel_id")
        receiver_id = data.get("receiver_id")

        payload = {
            "transfer_id": transfer_id,
            "filename": data.get("filename"),
            "file_size": data.get("file_size"),
            "timestamp": data.get("timestamp"),
        }

        # Broadcast to channel if group transfer
        if channel_id:
            await sio.emit(
                "file_drop:transfer_complete",
                payload,
                to=channel_id,
            )
        # Notify receiver if DM
        elif receiver_id:
            await sio.emit(
                "file_drop:transfer_complete",
                payload,
                to=receiver_id,
            )

        logger.info(
            "file_transfer_completed_broadcast",
            transfer_id=transfer_id,
            filename=data.get("filename"),
        )

    except Exception as e:
        logger.error("file_drop_complete_error", error=str(e))


@sio.event
async def file_drop_transfer_failed(sid: str, data: dict):
    """
    Notify that file transfer failed.
    Expected data: { transfer_id, error_message, channel_id|receiver_id }
    """
    try:
        transfer_id = data.get("transfer_id")
        channel_id = data.get("channel_id")
        receiver_id = data.get("receiver_id")

        payload = {
            "transfer_id": transfer_id,
            "error_message": data.get("error_message"),
            "timestamp": data.get("timestamp"),
        }

        # Broadcast to channel if group transfer
        if channel_id:
            await sio.emit(
                "file_drop:transfer_failed",
                payload,
                to=channel_id,
            )
        # Notify receiver if DM
        elif receiver_id:
            await sio.emit(
                "file_drop:transfer_failed",
                payload,
                to=receiver_id,
            )

        logger.warning(
            "file_transfer_failed_broadcast",
            transfer_id=transfer_id,
            error=data.get("error_message"),
        )

    except Exception as e:
        logger.error("file_drop_failed_error", error=str(e))


@sio.event
async def file_drop_offer(sid: str, data: dict):
    """
    Offer file to recipient(s) before transfer.
    Expected data: {
        filename | file_name,
        file_size,
        mime_type | file_type,
        recipient_id | receiver_id,
        channel_id,
        file_id (optional, when file already uploaded)
    }
    Allows recipient to accept/reject before transfer begins.
    """
    try:
        sender_id = await get_user_id(sid)
        filename = data.get("filename") or data.get("file_name")
        file_size = data.get("file_size")
        mime_type = data.get("mime_type") or data.get("file_type")
        receiver_id = data.get("receiver_id") or data.get("recipient_id")
        channel_id = data.get("channel_id")
        file_id = data.get("file_id")
        offer_id = data.get("offer_id") or f"offer-{int(__import__('time').time() * 1000)}-{sid[:6]}"

        payload = {
            "offer_id": offer_id,
            "sender_id": sender_id,
            "filename": filename,
            "file_name": filename,
            "file_size": file_size,
            "mime_type": mime_type,
            "file_type": mime_type,
            "file_id": file_id,
            "channel_id": channel_id,
            "timestamp": data.get("timestamp"),
        }

        # Send to recipient(s) — emit_to_user fans out across federation
        # so a recipient on a sibling Helen server still receives the offer.
        if receiver_id:
            await emit_to_user("filedrop:offer", payload, receiver_id)
            await emit_to_user("file_drop:offer", payload, receiver_id)
        elif channel_id:
            await sio.emit("filedrop:offer", payload, room=channel_id, skip_sid=sid)
            await sio.emit("file_drop:offer", payload, room=channel_id, skip_sid=sid)

        logger.debug(
            "file_offer_broadcast",
            offer_id=offer_id,
            filename=filename,
            file_size=file_size,
        )

        return {"status": "offered", "offer_id": offer_id}

    except Exception as e:
        logger.error("file_drop_offer_error", error=str(e))
        return {"error": str(e)}


# Colon-style alias for client compatibility
@sio.on("filedrop:offer")
async def filedrop_offer_alias(sid: str, data: dict):
    return await file_drop_offer(sid, data)


@sio.event
async def file_drop_accept(sid: str, data: dict):
    """
    Recipient accepts file offer.
    Expected data: { offer_id?, receiver_id?, sender_id?, filename?, timestamp?, download_path? }
    """
    try:
        recipient_id = await get_user_id(sid)
        sender_id = data.get("sender_id")
        offer_id = data.get("offer_id")
        filename = data.get("filename") or data.get("file_name")

        payload = {
            "offer_id": offer_id,
            "recipient_id": recipient_id,
            "filename": filename,
            "timestamp": data.get("timestamp"),
        }

        # Notify sender (look up by user_id when known, else echo to sid).
        # emit_to_user crosses federation when sender is on a sibling server.
        if sender_id:
            await emit_to_user("filedrop:accepted", payload, sender_id)
            await emit_to_user("file_drop:accepted", payload, sender_id)
        else:
            await sio.emit("filedrop:accepted", payload, to=sid)
            await sio.emit("file_drop:accepted", payload, to=sid)

        logger.debug("file_offer_accepted", offer_id=offer_id, recipient_id=recipient_id)
        return {"status": "accepted", "offer_id": offer_id}

    except Exception as e:
        logger.error("file_drop_accept_error", error=str(e))
        return {"error": str(e)}


@sio.on("filedrop:accept")
async def filedrop_accept_alias(sid: str, data: dict):
    return await file_drop_accept(sid, data)


@sio.event
async def file_drop_reject(sid: str, data: dict):
    """
    Recipient rejects file offer.
    Expected data: { offer_id?, sender_id?, receiver_id?, filename?, reason?, timestamp? }
    """
    try:
        recipient_id = await get_user_id(sid)
        sender_id = data.get("sender_id")
        offer_id = data.get("offer_id")
        filename = data.get("filename") or data.get("file_name")
        reason = data.get("reason", "User declined")

        payload = {
            "offer_id": offer_id,
            "recipient_id": recipient_id,
            "filename": filename,
            "reason": reason,
            "timestamp": data.get("timestamp"),
        }

        # Notify sender
        if sender_id:
            await emit_to_user("filedrop:rejected", payload, sender_id)
            await emit_to_user("file_drop:rejected", payload, sender_id)
        else:
            await sio.emit("filedrop:rejected", payload, to=sid)
            await sio.emit("file_drop:rejected", payload, to=sid)

        logger.debug("file_offer_rejected", offer_id=offer_id, recipient_id=recipient_id)
        return {"status": "rejected", "offer_id": offer_id}

    except Exception as e:
        logger.error("file_drop_reject_error", error=str(e))
        return {"error": str(e)}


@sio.on("filedrop:reject")
async def filedrop_reject_alias(sid: str, data: dict):
    return await file_drop_reject(sid, data)


@sio.on("filedrop:cancel")
async def filedrop_cancel(sid: str, data: dict):
    """
    Sender cancels an in-flight file transfer.
    Expected data: { transfer_id, recipient_id?, channel_id? }
    Notifies recipient(s) so they can stop downloading and clean up UI.
    """
    try:
        sender_id = await get_user_id(sid)
        transfer_id = data.get("transfer_id")
        recipient_id = data.get("recipient_id") or data.get("receiver_id")
        channel_id = data.get("channel_id")

        if not transfer_id:
            return {"error": "transfer_id is required"}

        payload = {
            "transfer_id": transfer_id,
            "sender_id": sender_id,
            "reason": data.get("reason", "Sender cancelled"),
            "timestamp": data.get("timestamp"),
        }

        if recipient_id:
            await emit_to_user("filedrop:cancelled", payload, recipient_id)
            await emit_to_user("file_drop:cancelled", payload, recipient_id)
        elif channel_id:
            await sio.emit("filedrop:cancelled", payload, room=channel_id, skip_sid=sid)
            await sio.emit("file_drop:cancelled", payload, room=channel_id, skip_sid=sid)

        logger.info("file_transfer_cancelled", transfer_id=transfer_id, sender_id=sender_id)
        return {"status": "cancelled", "transfer_id": transfer_id}

    except Exception as e:
        logger.error("file_drop_cancel_error", error=str(e))
        return {"error": str(e)}
