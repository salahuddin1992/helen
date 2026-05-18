"""
Channel auto-delete (TTL) endpoints.

  GET    /api/channels/{id}/ttl           — read current cap
  PUT    /api/channels/{id}/ttl           — set cap (channel admin)
  DELETE /api/channels/{id}/ttl           — clear cap (channel admin)
  POST   /api/channels/{id}/ttl/sweep-now — run a sweep on demand
                                            (admin only — useful
                                            for "delete everything
                                            older than the new cap
                                            without waiting 1h").

The cap is in **seconds**. The server pins the minimum to 60s and
the maximum to 30 days (see ``channel_message_ttl.set``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.channel import Channel, ChannelMember
from app.services.channel_message_ttl import (
    get_ttl_seconds, set_ttl_seconds, sweep_once,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/channels", tags=["channels", "ttl"])


async def _require_channel_admin(
    db: AsyncSession, channel_id: str, user_id: str,
) -> None:
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
    role = (await db.execute(
        select(ChannelMember.role).where(
            ChannelMember.channel_id == channel_id,
            ChannelMember.user_id == user_id,
        ),
    )).scalar_one_or_none()
    if role == "admin":
        return
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


@router.get("/{channel_id}/ttl")
async def get_ttl(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await _require_channel_member(db, channel_id, user_id)
    return {
        "channel_id": channel_id,
        "ttl_seconds": get_ttl_seconds(channel_id),
    }


class _SetBody(BaseModel):
    ttl_seconds: int = Field(ge=0, le=30 * 24 * 3600)


@router.put("/{channel_id}/ttl")
async def put_ttl(
    channel_id: str,
    body: _SetBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await _require_channel_admin(db, channel_id, user_id)
    applied = set_ttl_seconds(channel_id, body.ttl_seconds)
    return {"channel_id": channel_id, "ttl_seconds": applied}


@router.delete("/{channel_id}/ttl", status_code=204)
async def delete_ttl(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await _require_channel_admin(db, channel_id, user_id)
    set_ttl_seconds(channel_id, 0)
    return None


@router.post("/{channel_id}/ttl/sweep-now")
async def trigger_sweep(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a sweep. Useful right after lowering the
    cap so the operator doesn't have to wait for the periodic
    pass."""
    await _require_channel_admin(db, channel_id, user_id)
    summary = await sweep_once()
    return {"ok": True, **summary}


__all__ = ["router"]
