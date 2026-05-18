"""
Channel-join-by-code endpoint.

Closes the loop on the desktop's invite-link feature: a user pastes
or clicks a link, the client POSTs the code here, and we:

  1. Redeem the code via ``access_codes_service`` (rate-limit, expiry,
     max-uses, revocation all enforced there).
  2. Resolve the target channel from the code's
     ``target_channel_id``.
  3. Self-add the redeemer as a member, **bypassing the
     channel-admin requirement** that ``ChannelService.add_member``
     normally enforces — this is the whole point of an invite link:
     the *code* is the authorization token, not the joining user's
     existing rights.
  4. Return the channel object so the client can navigate straight
     into the new chat.

This route is deliberately *not* under ``/api/admin/*`` — every
authenticated user can call it. Server-side audit logs the
``channel_invite_redeemed`` event with the code and joiner.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.channel import Channel, ChannelMember
from app.services.access_codes_service import get_service as get_codes_service

logger = get_logger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])


class _JoinBody(BaseModel):
    code: str


@router.post("/join-by-code")
async def join_by_code(
    body: _JoinBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    code = (body.code or "").strip()
    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty code",
        )

    # 1. Redeem the code (one-shot — failures don't decrement
    # uses_remaining inside redeem()).
    ok, reason, record = get_codes_service().redeem(code, user_id)
    if not ok:
        # Map machine-readable reasons to HTTP statuses operators
        # can act on. 410 for expired/revoked, 409 for exhausted /
        # self-redeem, 404 for unknown codes.
        status_map = {
            "not_found": status.HTTP_404_NOT_FOUND,
            "expired": status.HTTP_410_GONE,
            "revoked": status.HTTP_410_GONE,
            "exhausted": status.HTTP_409_CONFLICT,
            "self_redeem_forbidden": status.HTTP_409_CONFLICT,
        }
        raise HTTPException(
            status_code=status_map.get(reason, status.HTTP_400_BAD_REQUEST),
            detail=reason,
        )

    if not record or record.get("kind") != "invite":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="not an invite code",
        )

    target_channel_id = record.get("target_channel_id")
    if not target_channel_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invite code has no target channel",
        )

    # 2. Resolve the channel — must exist and not be a DM.
    channel = (await db.execute(
        select(Channel).where(Channel.id == target_channel_id),
    )).scalar_one_or_none()
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="target channel no longer exists",
        )
    if (channel.type or "").lower() == "dm":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot invite into a DM",
        )

    # 3. Already a member? Idempotent — return the channel without
    # creating a duplicate membership.
    existing = (await db.execute(
        select(ChannelMember).where(
            ChannelMember.channel_id == target_channel_id,
            ChannelMember.user_id == user_id,
        ),
    )).scalar_one_or_none()
    if existing is None:
        member = ChannelMember(
            channel_id=target_channel_id,
            user_id=user_id,
            role="member",
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)
        logger.info("channel_invite_redeemed",
                    code=code, channel_id=target_channel_id,
                    user_id=user_id)

    return {
        "ok": True,
        "channel_id": target_channel_id,
        "channel_name": channel.name,
        "channel_type": channel.type,
        "already_member": existing is not None,
    }


__all__ = ["router"]
