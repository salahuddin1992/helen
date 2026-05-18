"""
Scheduled message REST endpoints — schedule, list, edit, cancel future messages.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.services.scheduled_message_service import ScheduledMessageService

router = APIRouter(prefix="/scheduled-messages", tags=["scheduled-messages"])


class ScheduledMessageCreate(BaseModel):
    channel_id: str
    content: str = Field(..., min_length=0, max_length=10000)
    send_at: datetime
    msg_type: str = Field("text", pattern=r"^(text|file|image|reply|system)$")
    reply_to: str | None = None
    file_id: str | None = None


class ScheduledMessageUpdate(BaseModel):
    content: str | None = Field(None, max_length=10000)
    send_at: datetime | None = None


@router.post("", status_code=201)
async def create_scheduled_message(
    body: ScheduledMessageCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        scheduled = await ScheduledMessageService.schedule(
            db,
            sender_id=user_id,
            channel_id=body.channel_id,
            content=body.content,
            send_at=body.send_at,
            msg_type=body.msg_type,
            reply_to=body.reply_to,
            file_id=body.file_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return scheduled.to_dict()


@router.get("")
async def list_scheduled_messages(
    status: str | None = Query(None, pattern=r"^(pending|sent|failed|cancelled)$"),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    items = await ScheduledMessageService.list_for_user(
        db, sender_id=user_id, status=status, limit=limit
    )
    return {"results": [s.to_dict() for s in items], "total": len(items)}


@router.patch("/{scheduled_id}")
async def update_scheduled_message(
    scheduled_id: str,
    body: ScheduledMessageUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        scheduled = await ScheduledMessageService.update(
            db,
            scheduled_id=scheduled_id,
            sender_id=user_id,
            content=body.content,
            send_at=body.send_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return scheduled.to_dict()


@router.delete("/{scheduled_id}")
async def cancel_scheduled_message(
    scheduled_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        scheduled = await ScheduledMessageService.cancel(
            db, scheduled_id=scheduled_id, sender_id=user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return scheduled.to_dict()
