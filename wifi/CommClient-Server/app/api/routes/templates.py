"""
Message template (quick reply) REST endpoints.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.services.template_service import TemplateService

router = APIRouter(prefix="/templates", tags=["templates"])


class TemplateCreate(BaseModel):
    shortcut: str = Field(..., min_length=1, max_length=64)
    content: str = Field(..., min_length=1, max_length=4_000)
    title: str | None = Field(None, max_length=128)
    channel_id: str | None = None


class TemplateUpdate(BaseModel):
    shortcut: str | None = Field(None, min_length=1, max_length=64)
    content: str | None = Field(None, min_length=1, max_length=4_000)
    title: str | None = Field(None, max_length=128)


class TemplateResponse(BaseModel):
    id: str
    owner_id: str
    channel_id: str | None
    scope: str
    shortcut: str
    title: str | None
    content: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.post("", response_model=TemplateResponse, status_code=201)
async def create_template(
    body: TemplateCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await TemplateService.create(
            db,
            owner_id=user_id,
            shortcut=body.shortcut,
            content=body.content,
            title=body.title,
            channel_id=body.channel_id,
        )
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return TemplateResponse.model_validate(rec)


@router.get("")
async def list_templates(
    channel_id: str | None = Query(None),
    q: str | None = Query(None, max_length=128),
    limit: int = Query(100, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        items = await TemplateService.list_for_user(
            db, user_id, channel_id=channel_id, query=q, limit=limit
        )
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {
        "items": [TemplateResponse.model_validate(i) for i in items],
        "total": len(items),
    }


@router.get("/resolve")
async def resolve_template(
    shortcut: str = Query(..., min_length=1, max_length=64),
    channel_id: str | None = Query(None),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await TemplateService.resolve(
            db, user_id, shortcut, channel_id=channel_id
        )
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if rec is None:
        return {"resolved": None}
    return {"resolved": TemplateResponse.model_validate(rec)}


@router.get("/{template_id}", response_model=TemplateResponse)
async def get_template(
    template_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await TemplateService.get(db, template_id, user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Template not found")
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return TemplateResponse.model_validate(rec)


@router.patch("/{template_id}", response_model=TemplateResponse)
async def update_template(
    template_id: str,
    body: TemplateUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await TemplateService.update(
            db,
            template_id,
            user_id,
            shortcut=body.shortcut,
            title=body.title,
            content=body.content,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Template not found")
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return TemplateResponse.model_validate(rec)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        await TemplateService.delete(db, template_id, user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Template not found")
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return None
