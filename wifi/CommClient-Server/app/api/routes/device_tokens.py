"""
Device push token REST endpoints — register/list/deactivate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import NotFoundError, ValidationError
from app.services.device_token_service import DeviceTokenService

router = APIRouter(prefix="/device-tokens", tags=["push"])


class DeviceTokenRegister(BaseModel):
    provider: Literal["fcm", "apns", "web"]
    token: str = Field(..., min_length=1, max_length=512)
    platform: Literal["ios", "android", "web", "desktop"]
    device_name: str | None = Field(None, max_length=128)
    app_version: str | None = Field(None, max_length=32)
    bundle_id: str | None = Field(None, max_length=128)
    extra_json: str | None = Field(None, max_length=1024)


class DeviceTokenDeactivateByValue(BaseModel):
    provider: Literal["fcm", "apns", "web"]
    token: str = Field(..., min_length=1, max_length=512)


class DeviceTokenResponse(BaseModel):
    id: str
    provider: str
    platform: str
    device_name: str | None = None
    app_version: str | None = None
    bundle_id: str | None = None
    is_active: bool
    last_used_at: datetime | None = None
    failure_count: int = 0
    last_error: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


def _to_response(t) -> DeviceTokenResponse:
    return DeviceTokenResponse.model_validate(t)


@router.post("", response_model=DeviceTokenResponse, status_code=201)
async def register_token(
    body: DeviceTokenRegister,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        record = await DeviceTokenService.register(
            db,
            user_id=user_id,
            provider=body.provider,
            token=body.token,
            platform=body.platform,
            device_name=body.device_name,
            app_version=body.app_version,
            bundle_id=body.bundle_id,
            extra_json=body.extra_json,
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _to_response(record)


@router.get("", response_model=list[DeviceTokenResponse])
async def list_tokens(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    records = await DeviceTokenService.list_for_user(db, user_id)
    return [_to_response(r) for r in records]


@router.delete("/{token_id}", status_code=204)
async def delete_token(
    token_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        await DeviceTokenService.deactivate(db, user_id, token_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Device token not found")
    return None


@router.post("/deactivate", status_code=204)
async def deactivate_by_value(
    body: DeviceTokenDeactivateByValue,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate by raw token value — used when a client logs out."""
    await DeviceTokenService.deactivate_by_token(
        db, user_id, body.provider, body.token
    )
    return None
