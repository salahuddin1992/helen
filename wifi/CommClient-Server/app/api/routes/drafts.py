"""
Message drafts REST endpoints — per-user, per-channel saved drafts.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.services.draft_service import DraftService

router = APIRouter(prefix="/drafts", tags=["drafts"])


class DraftUpsert(BaseModel):
    channel_id: str
    content: str = Field("", max_length=16_000)
    thread_root_id: str | None = None
    extra_json: str | None = Field(None, max_length=8_000)


class DraftResponse(BaseModel):
    id: str
    channel_id: str
    thread_root_id: str | None = None
    content: str
    extra_json: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.put("", response_model=DraftResponse)
async def upsert_draft(
    body: DraftUpsert,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await DraftService.upsert(
            db,
            user_id=user_id,
            channel_id=body.channel_id,
            content=body.content,
            thread_root_id=body.thread_root_id,
            extra_json=body.extra_json,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return DraftResponse.model_validate(rec)


@router.get("")
async def list_drafts(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    items = await DraftService.list_for_user(db, user_id)
    return {
        "items": [DraftResponse.model_validate(i) for i in items],
        "total": len(items),
    }


@router.get("/by-channel")
async def get_draft_by_channel(
    channel_id: str = Query(...),
    thread_root_id: str | None = Query(None),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    rec = await DraftService.get(db, user_id, channel_id, thread_root_id)
    if rec is None:
        return {"draft": None}
    return {"draft": DraftResponse.model_validate(rec)}


@router.delete("/by-channel", status_code=204)
async def delete_draft_by_channel(
    channel_id: str = Query(...),
    thread_root_id: str | None = Query(None),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    removed = await DraftService.delete(db, user_id, channel_id, thread_root_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Draft not found")
    return None


@router.delete("/{draft_id}", status_code=204)
async def delete_draft(
    draft_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    removed = await DraftService.delete_by_id(db, user_id, draft_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Draft not found")
    return None
