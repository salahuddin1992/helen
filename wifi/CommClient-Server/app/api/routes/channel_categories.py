"""
Channel category (folder) REST endpoints — per-user channel grouping.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.services.channel_category_service import ChannelCategoryService

router = APIRouter(prefix="/channel-categories", tags=["channel-categories"])


class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    color: str | None = Field(None, max_length=16)
    sort_order: int | None = None


class CategoryUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    sort_order: int | None = None
    is_collapsed: bool | None = None
    color: str | None = Field(None, max_length=16)


class CategoryReorder(BaseModel):
    ordered_ids: list[str]


class CategoryAssignment(BaseModel):
    channel_id: str
    sort_order: int | None = None


class AssignmentResponse(BaseModel):
    user_id: str
    channel_id: str
    category_id: str
    sort_order: int

    class Config:
        from_attributes = True


class CategoryResponse(BaseModel):
    id: str
    name: str
    sort_order: int
    is_collapsed: bool
    color: str | None
    created_at: datetime
    channel_count: int = 0

    class Config:
        from_attributes = True


def _to_response(rec, channel_count: int = 0) -> CategoryResponse:
    return CategoryResponse(
        id=rec.id,
        name=rec.name,
        sort_order=rec.sort_order,
        is_collapsed=rec.is_collapsed,
        color=rec.color,
        created_at=rec.created_at,
        channel_count=channel_count,
    )


@router.post("", response_model=CategoryResponse, status_code=201)
async def create_category(
    body: CategoryCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await ChannelCategoryService.create(
            db, user_id, body.name, color=body.color, sort_order=body.sort_order
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _to_response(rec, channel_count=0)


@router.get("")
async def list_categories(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    items = await ChannelCategoryService.list_for_user(db, user_id)
    return {
        "items": [_to_response(i, channel_count=len(i.assignments)) for i in items],
        "total": len(items),
    }


@router.patch("/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: str,
    body: CategoryUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await ChannelCategoryService.update(
            db,
            category_id,
            user_id,
            name=body.name,
            sort_order=body.sort_order,
            is_collapsed=body.is_collapsed,
            color=body.color,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Category not found")
    except ForbiddenError:
        raise HTTPException(status_code=403, detail="Forbidden")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    assignments = await ChannelCategoryService.list_assignments(
        db, user_id, category_id
    )
    return _to_response(rec, channel_count=len(assignments))


@router.delete("/{category_id}", status_code=204)
async def delete_category(
    category_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        await ChannelCategoryService.delete(db, category_id, user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Category not found")
    except ForbiddenError:
        raise HTTPException(status_code=403, detail="Forbidden")
    return None


@router.post("/reorder")
async def reorder_categories(
    body: CategoryReorder,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        items = await ChannelCategoryService.reorder(db, user_id, body.ordered_ids)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "items": [_to_response(i, channel_count=len(i.assignments)) for i in items],
        "total": len(items),
    }


@router.post(
    "/{category_id}/channels",
    response_model=AssignmentResponse,
    status_code=201,
)
async def assign_channel(
    category_id: str,
    body: CategoryAssignment,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await ChannelCategoryService.assign_channel(
            db, user_id, category_id, body.channel_id, sort_order=body.sort_order
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Category not found")
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return AssignmentResponse.model_validate(rec)


@router.delete("/channels/{channel_id}", status_code=204)
async def unassign_channel(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    removed = await ChannelCategoryService.unassign_channel(db, user_id, channel_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Channel not in any category")
    return None
