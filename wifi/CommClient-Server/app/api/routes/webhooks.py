"""
Webhook REST endpoints — register/list/update/delete + delivery audit.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import NotFoundError, ValidationError
from app.models.webhook import WebhookDelivery
from app.services.webhook_service import WebhookService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    url: str = Field(..., min_length=8, max_length=2048)
    events: list[str] | None = None
    channel_id: str | None = None
    secret: str | None = Field(None, min_length=8, max_length=128)


class WebhookUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    url: str | None = Field(None, min_length=8, max_length=2048)
    events: list[str] | None = None
    channel_id: str | None = None
    is_active: bool | None = None


class WebhookResponse(BaseModel):
    id: str
    name: str
    url: str
    events: str
    channel_id: str | None = None
    is_active: bool
    consecutive_failures: int
    last_delivery_at: datetime | None = None
    last_status: int | None = None
    last_error: str | None = None
    created_at: datetime
    secret: str | None = None  # only returned on create

    class Config:
        from_attributes = True


def _to_response(rec, *, include_secret: bool = False) -> WebhookResponse:
    payload = WebhookResponse.model_validate(rec)
    if not include_secret:
        payload.secret = None
    else:
        payload.secret = rec.secret
    return payload


@router.post("", response_model=WebhookResponse, status_code=201)
async def create_webhook(
    body: WebhookCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await WebhookService.create(
            db,
            owner_id=user_id,
            name=body.name,
            url=body.url,
            events=body.events,
            channel_id=body.channel_id,
            secret=body.secret,
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Return the secret on create only — caller must store it
    return _to_response(rec, include_secret=True)


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    items = await WebhookService.list_for_owner(db, user_id)
    return [_to_response(i) for i in items]


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    webhook_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await WebhookService.get(db, webhook_id, user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return _to_response(rec)


@router.patch("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: str,
    body: WebhookUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        rec = await WebhookService.update(
            db,
            webhook_id,
            user_id,
            name=body.name,
            url=body.url,
            events=body.events,
            is_active=body.is_active,
            channel_id=body.channel_id,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Webhook not found")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _to_response(rec)


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        await WebhookService.delete(db, webhook_id, user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return None


@router.get("/{webhook_id}/deliveries")
async def list_deliveries(
    webhook_id: str,
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        await WebhookService.get(db, webhook_id, user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Webhook not found")
    result = await db.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_id == webhook_id)
        .order_by(desc(WebhookDelivery.created_at))
        .limit(limit)
    )
    items = []
    for d in result.scalars().all():
        items.append(
            {
                "id": d.id,
                "event": d.event,
                "status": d.status,
                "attempt_count": d.attempt_count,
                "last_status_code": d.last_status_code,
                "last_error": d.last_error,
                "delivered_at": d.delivered_at,
                "next_attempt_at": d.next_attempt_at,
                "created_at": d.created_at,
            }
        )
    return {"deliveries": items}
