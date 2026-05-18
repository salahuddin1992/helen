"""
REST endpoints for per-recipient file acceptance tracking.

Mounted under the existing ``/files`` namespace to keep URLs clustered
with the rest of the file API.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_permission_denied
from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.file_acceptance import (
    STATE_ACCEPTED,
    STATE_DELIVERED,
    STATE_REJECTED,
    VALID_STATES,
)
from app.services.channel_service import ChannelService
from app.services.file_acceptance_service import FileAcceptanceService
from app.services.file_service import FileService

logger = get_logger(__name__)

router = APIRouter(prefix="/files", tags=["files", "acceptance"])


class StateUpdateBody(BaseModel):
    state: str = Field(..., pattern=r"^(delivered|accepted|rejected)$")
    bytes_received: int | None = Field(default=None, ge=0)


@router.post("/{file_id}/acceptance", status_code=status.HTTP_200_OK)
async def set_acceptance_state(
    file_id: str,
    body: StateUpdateBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Update the current user's acceptance state for a shared file.

    The file must be attached to a channel the caller is a member of.
    The caller cannot update state for another recipient.
    """
    record = await FileService.get_file(db, file_id)

    if not record.channel_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="file is not attached to a channel",
        )

    is_member = await ChannelService.is_member(db, record.channel_id, user_id)
    if not is_member:
        audit_permission_denied(user_id, f"file:{file_id}", "acceptance")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="you are not a member of this file's channel",
        )

    if record.uploader_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="uploader cannot update their own acceptance state",
        )

    # Bootstrap the row if it doesn't exist (e.g. the user joined the
    # channel after the file was shared).
    await FileAcceptanceService.ensure_rows_for_channel_file(
        db,
        file_id=file_id,
        channel_id=record.channel_id,
        uploader_id=record.uploader_id,
    )

    try:
        row, advanced = await FileAcceptanceService.set_state(
            db,
            file_id=file_id,
            recipient_id=user_id,
            target=body.state,
            bytes_received=body.bytes_received,
        )
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e))

    await db.commit()
    await db.refresh(row)

    # Fan out a socket event so the uploader's UI updates live.
    try:
        await _emit_state_changed(record, row, advanced)
    except Exception as exc:  # best-effort — don't fail the API
        logger.warning("file_acceptance_emit_failed", file_id=file_id, error=str(exc))

    logger.info(
        "file_acceptance_state_updated",
        file_id=file_id,
        recipient_id=user_id,
        state=row.state,
        advanced=advanced,
    )
    return row.to_dict()


@router.get("/{file_id}/acceptance")
async def get_file_acceptance_summary(
    file_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Aggregate summary of delivery/acceptance state for a shared file.

    Available to the uploader and to any channel member of the carrying
    channel.
    """
    record = await FileService.get_file(db, file_id)

    if record.uploader_id != user_id:
        if not record.channel_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="access denied")
        is_member = await ChannelService.is_member(db, record.channel_id, user_id)
        if not is_member:
            audit_permission_denied(user_id, f"file:{file_id}", "acceptance_summary")
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="access denied")

    summary = await FileAcceptanceService.summary(db, file_id)
    return summary


# Mounted separately at /files/acceptance (without file_id prefix) so
# the ambiguous path isn't captured by /files/{file_id}.
inbox_router = APIRouter(prefix="/files/acceptance", tags=["files", "acceptance"])


@inbox_router.get("/inbox")
async def get_pending_inbox(
    state: str | None = Query(None, pattern=r"^(pending|delivered|accepted|rejected)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    List files waiting for the caller's action (default: pending + delivered).

    Pass ``state`` to narrow the query to a specific bucket.
    """
    if state is None:
        rows = await FileAcceptanceService.pending_for_recipient(db, user_id, limit=limit)
    else:
        rows = await FileAcceptanceService.list_for_recipient(
            db, user_id, states=[state], limit=limit, offset=offset,
        )
    return {
        "total": len(rows),
        "items": [r.to_dict() for r in rows],
    }


# ── Internal helpers ─────────────────────────────────────────────────


async def _emit_state_changed(record, row, advanced: bool) -> None:
    """
    Fan out a ``file_acceptance:updated`` event to the channel so both
    the uploader and the recipient's other devices stay in sync.

    Lazy-imported to avoid a hard dep on the socket server during unit
    tests that only exercise services.
    """
    from app.socket.server import sio
    if not sio:
        return
    payload = {
        "file_id": row.file_id,
        "channel_id": row.channel_id,
        "recipient_id": row.recipient_id,
        "state": row.state,
        "advanced": advanced,
        "bytes_received": row.bytes_received,
        "acted_at": row.acted_at.isoformat() if row.acted_at else None,
        "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
    }
    # Broadcast to the channel room — all members see the update.
    try:
        await sio.emit(
            "file_acceptance:updated",
            payload,
            room=f"channel:{row.channel_id}",
        )
    except Exception:
        # Fallback: emit without room if room addressing isn't wired.
        await sio.emit("file_acceptance:updated", payload)
