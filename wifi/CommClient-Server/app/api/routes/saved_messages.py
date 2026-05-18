"""
Saved (bookmarked) messages REST endpoints.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import NotFoundError, ValidationError
from app.services.saved_message_service import SavedMessageService

router = APIRouter(prefix="/saved-messages", tags=["saved-messages"])


class SavedMessageCreate(BaseModel):
    message_id: str
    folder: str | None = Field(None, max_length=64)
    note: str | None = Field(None, max_length=1024)


class SavedMessageUpdate(BaseModel):
    folder: str | None = Field(None, max_length=64)
    note: str | None = Field(None, max_length=1024)


class SavedMessageResponse(BaseModel):
    id: str
    message_id: str
    folder: str | None = None
    note: str | None = None
    created_at: datetime
    # Inlined message preview
    content: str | None = None
    sender_username: str | None = None
    channel_id: str | None = None

    class Config:
        from_attributes = True


def _to_response(rec) -> SavedMessageResponse:
    msg = getattr(rec, "message", None)
    sender = getattr(msg, "sender", None) if msg else None
    return SavedMessageResponse(
        id=rec.id,
        message_id=rec.message_id,
        folder=rec.folder,
        note=rec.note,
        created_at=rec.created_at,
        content=msg.content if msg else None,
        sender_username=sender.username if sender else None,
        channel_id=msg.channel_id if msg else None,
    )


@router.post("", response_model=SavedMessageResponse, status_code=201)
async def create_saved_message(
    body: SavedMessageCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await SavedMessageService.save(
            db, user_id, body.message_id, folder=body.folder, note=body.note
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Message not found")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Reload with relationships eager-loaded for response
    items, _ = await SavedMessageService.list_for_user(db, user_id, limit=1)
    matching = next((i for i in items if i.id == rec.id), rec)
    return _to_response(matching)


@router.get("")
async def list_saved_messages(
    folder: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    items, total = await SavedMessageService.list_for_user(
        db, user_id, folder=folder, limit=limit, offset=offset
    )
    return {
        "items": [_to_response(i) for i in items],
        "total": total,
    }


@router.get("/folders")
async def list_folders(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    folders = await SavedMessageService.list_folders(db, user_id)
    return {"folders": folders}


@router.patch("/{message_id}", response_model=SavedMessageResponse)
async def update_saved_message(
    message_id: str,
    body: SavedMessageUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await SavedMessageService.update_note(
            db, user_id, message_id, folder=body.folder, note=body.note
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Saved message not found")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    items, _ = await SavedMessageService.list_for_user(db, user_id, limit=200)
    matching = next((i for i in items if i.id == rec.id), rec)
    return _to_response(matching)


@router.delete("/{message_id}", status_code=204)
async def delete_saved_message(
    message_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    removed = await SavedMessageService.unsave(db, user_id, message_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Saved message not found")
    return None
