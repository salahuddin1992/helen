"""
User availability schedule REST endpoints — recurring weekly windows + away
auto-reply text.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import NotFoundError, ValidationError
from app.services.schedule_service import ScheduleService

router = APIRouter(prefix="/schedule", tags=["schedule"])


class RuleCreate(BaseModel):
    weekday: int = Field(..., ge=0, le=6)
    start_minute: int = Field(..., ge=0, le=1439)
    end_minute: int = Field(..., gt=0, le=1440)
    status: str = Field("available", min_length=1, max_length=32)
    label: str | None = Field(None, max_length=128)


class RuleUpdate(BaseModel):
    weekday: int | None = Field(None, ge=0, le=6)
    start_minute: int | None = Field(None, ge=0, le=1439)
    end_minute: int | None = Field(None, gt=0, le=1440)
    status: str | None = Field(None, min_length=1, max_length=32)
    label: str | None = Field(None, max_length=128)


class RuleResponse(BaseModel):
    id: str
    weekday: int
    start_minute: int
    end_minute: int
    status: str
    label: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class AwayMessageBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    is_active: bool = True
    mode: str = Field("schedule", pattern="^(schedule|always_on|always_away)$")


class AwayMessageResponse(BaseModel):
    id: str
    text: str
    is_active: bool
    mode: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ── Rules ─────────────────────────────────────────────────


@router.post("/rules", response_model=RuleResponse, status_code=201)
async def add_rule(
    body: RuleCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await ScheduleService.add_rule(
            db,
            user_id,
            body.weekday,
            body.start_minute,
            body.end_minute,
            status=body.status,
            label=body.label,
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RuleResponse.model_validate(rec)


@router.get("/rules")
async def list_rules(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    items = await ScheduleService.list_rules(db, user_id)
    return {
        "items": [RuleResponse.model_validate(i) for i in items],
        "total": len(items),
    }


@router.patch("/rules/{rule_id}", response_model=RuleResponse)
async def update_rule(
    rule_id: str,
    body: RuleUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await ScheduleService.update_rule(
            db,
            rule_id,
            user_id,
            weekday=body.weekday,
            start_minute=body.start_minute,
            end_minute=body.end_minute,
            status=body.status,
            label=body.label,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Rule not found")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RuleResponse.model_validate(rec)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    removed = await ScheduleService.delete_rule(db, rule_id, user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Rule not found")
    return None


@router.delete("/rules", status_code=200)
async def clear_rules(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    n = await ScheduleService.clear_rules(db, user_id)
    return {"deleted": n}


# ── Away message ──────────────────────────────────────────


@router.put("/away", response_model=AwayMessageResponse)
async def set_away(
    body: AwayMessageBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await ScheduleService.set_away_message(
            db, user_id, body.text, is_active=body.is_active, mode=body.mode
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AwayMessageResponse.model_validate(rec)


@router.get("/away")
async def get_away(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    rec = await ScheduleService.get_away_message(db, user_id)
    if rec is None:
        return {"away": None}
    return {"away": AwayMessageResponse.model_validate(rec)}


@router.delete("/away", status_code=204)
async def clear_away(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    removed = await ScheduleService.clear_away_message(db, user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No away message set")
    return None


@router.get("/me/status")
async def my_status(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    return await ScheduleService.resolve_status(db, user_id)


@router.get("/{target_user_id}/status")
async def user_status(
    target_user_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    return await ScheduleService.resolve_status(db, target_user_id)
