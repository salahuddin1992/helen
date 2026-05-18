"""
Channel slow-mode endpoints.

Three operations:
  GET  /api/channels/{id}/slow-mode      — read current cap
  PUT  /api/channels/{id}/slow-mode      — set cap (channel admin)
  DELETE /api/channels/{id}/slow-mode    — clear cap (channel admin)

All members can READ the cap so the client UI knows whether to
render a "slow mode: 30s" hint and a countdown after sending. Only
channel admins (the creator + anyone with role="admin" in
``channel_members``) can change it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.channel import Channel, ChannelMember
from app.services.channel_slow_mode import (
    get_slow_mode_seconds, set_slow_mode_seconds,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/channels", tags=["channels", "slow-mode"])


async def _require_channel_admin(
    db: AsyncSession, channel_id: str, user_id: str,
) -> None:
    """Allow if the user is the channel creator OR has role=admin
    in the channel_members table OR is a site-level admin."""
    channel = (await db.execute(
        select(Channel).where(Channel.id == channel_id),
    )).scalar_one_or_none()
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="channel not found",
        )
    if channel.created_by == user_id:
        return
    role_row = await db.execute(
        select(ChannelMember.role).where(
            ChannelMember.channel_id == channel_id,
            ChannelMember.user_id == user_id,
        ),
    )
    role = role_row.scalar_one_or_none()
    if role == "admin":
        return
    # Final escape hatch: site admin (User.role == "admin").
    from app.models.user import User
    site_role = (await db.execute(
        select(User.role).where(User.id == user_id),
    )).scalar_one_or_none()
    if site_role == "admin":
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="not channel admin",
    )


async def _require_channel_member(
    db: AsyncSession, channel_id: str, user_id: str,
) -> None:
    member = (await db.execute(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel_id,
            ChannelMember.user_id == user_id,
        ),
    )).scalar_one_or_none()
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not a member",
        )


@router.get("/{channel_id}/slow-mode")
async def get_slow_mode(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await _require_channel_member(db, channel_id, user_id)
    return {
        "channel_id": channel_id,
        "seconds_per_message": get_slow_mode_seconds(channel_id),
    }


class _SetBody(BaseModel):
    seconds_per_message: int = Field(ge=0, le=21600)


@router.put("/{channel_id}/slow-mode")
async def put_slow_mode(
    channel_id: str,
    body: _SetBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await _require_channel_admin(db, channel_id, user_id)
    applied = set_slow_mode_seconds(channel_id, body.seconds_per_message)
    return {
        "channel_id": channel_id,
        "seconds_per_message": applied,
    }


@router.delete("/{channel_id}/slow-mode", status_code=204)
async def delete_slow_mode(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await _require_channel_admin(db, channel_id, user_id)
    set_slow_mode_seconds(channel_id, 0)
    return None


__all__ = ["router"]
