"""
REST endpoints for multicast / group-file offers.

These sit next to the existing ``/files`` namespace and expose the same
primitives as the Socket.IO handlers so non-socket clients (mobile, CLI
tools, admin dashboards) can drive the flow end-to-end.

Routes
------
  POST   /channels/{channel_id}/group-file-offers        — create
  GET    /channels/{channel_id}/group-file-offers        — list (channel scope)
  GET    /group-file-offers/inbox                         — my pending offers
  GET    /group-file-offers/{offer_id}                    — read
  GET    /group-file-offers/{offer_id}/stats              — per-offer dashboard
  POST   /group-file-offers/{offer_id}/accept             — recipient action
  POST   /group-file-offers/{offer_id}/reject             — recipient action
  POST   /group-file-offers/{offer_id}/chunks/{idx}       — report chunk received
  GET    /group-file-offers/{offer_id}/chunks/{idx}/peers — swarm lookup
  DELETE /group-file-offers/{offer_id}                    — cancel (sender only)
  POST   /group-file-offers/_sweep-expired                — admin / cron
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.group_file_offer import (
    OFFER_VALID_STATUSES,
)
from app.services.channel_service import ChannelService
from app.services.group_file_service import (
    GroupFileService,
    MAX_OFFER_TTL,
)

logger = get_logger(__name__)

router = APIRouter(tags=["group-file-offers"])


# ── Bodies ─────────────────────────────────────────────────────────

class CreateOfferBody(BaseModel):
    file_id: str = Field(..., min_length=1, max_length=32)
    chunk_size: int = Field(..., gt=0)
    total_chunks: int = Field(..., gt=0)
    caption: str | None = Field(default=None, max_length=2000)
    swarm_enabled: bool = Field(default=True)
    expires_in_sec: int | None = Field(
        default=None, gt=0, le=int(MAX_OFFER_TTL.total_seconds()),
    )
    checksum: str | None = Field(default=None, max_length=128)


class ReportChunkBody(BaseModel):
    chunk_bytes: int | None = Field(default=None, ge=0)


# ── Helpers ────────────────────────────────────────────────────────

def _map_err(e: Exception) -> HTTPException:
    if isinstance(e, NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    if isinstance(e, ForbiddenError):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    if isinstance(e, ValidationError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))


# ── Create ─────────────────────────────────────────────────────────

@router.post(
    "/channels/{channel_id}/group-file-offers",
    status_code=status.HTTP_201_CREATED,
)
async def create_offer(
    channel_id: str,
    body: CreateOfferBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        offer = await GroupFileService.create_offer(
            db,
            sender_id=user_id,
            channel_id=channel_id,
            file_id=body.file_id,
            chunk_size=body.chunk_size,
            total_chunks=body.total_chunks,
            caption=body.caption,
            swarm_enabled=body.swarm_enabled,
            expires_in=(
                timedelta(seconds=body.expires_in_sec)
                if body.expires_in_sec else None
            ),
            checksum=body.checksum,
        )
        return offer.to_dict()
    except (NotFoundError, ForbiddenError, ValidationError) as e:
        raise _map_err(e)


# ── List (channel) ────────────────────────────────────────────────

@router.get("/channels/{channel_id}/group-file-offers")
async def list_channel_offers(
    channel_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not await ChannelService.is_member(db, channel_id, user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "not a member of the channel")
    if status_filter and status_filter not in OFFER_VALID_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"invalid status '{status_filter}'")
    offers = await GroupFileService.list_offers_for_channel(
        db, channel_id, status=status_filter, limit=limit, offset=offset,
    )
    return {
        "channel_id": channel_id,
        "count": len(offers),
        "offers": [o.to_dict() for o in offers],
    }


# ── Inbox (per user) ──────────────────────────────────────────────

@router.get("/group-file-offers/inbox")
async def list_inbox(
    active_only: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=500),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    offers = await GroupFileService.list_offers_for_user(
        db, user_id, active_only=active_only, limit=limit,
    )
    return {"count": len(offers), "offers": [o.to_dict() for o in offers]}


# ── Read ──────────────────────────────────────────────────────────

@router.get("/group-file-offers/{offer_id}")
async def get_offer(
    offer_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        offer = await GroupFileService.get_offer(db, offer_id)
    except NotFoundError as e:
        raise _map_err(e)

    # Caller must be sender or a channel member.
    if offer.sender_id != user_id and not await ChannelService.is_member(
        db, offer.channel_id, user_id,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "not a participant of the offer")
    return offer.to_dict()


@router.get("/group-file-offers/{offer_id}/stats")
async def get_offer_stats(
    offer_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        offer = await GroupFileService.get_offer(db, offer_id)
    except NotFoundError as e:
        raise _map_err(e)

    # Only sender or channel admin can see full stats (rejected count
    # leaks individual decisions otherwise).
    if offer.sender_id != user_id:
        channel = await ChannelService.get_channel(db, offer.channel_id)
        is_admin = any(
            m.user_id == user_id and (m.role or "").lower() in ("admin", "owner")
            for m in (channel.members or [])
        )
        if not is_admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "stats restricted to sender / channel admin")
    return await GroupFileService.get_offer_stats(db, offer_id)


# ── Accept / reject ───────────────────────────────────────────────

@router.post("/group-file-offers/{offer_id}/accept")
async def accept_offer(
    offer_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        offer, row = await GroupFileService.accept_offer(db, offer_id, user_id)
    except (NotFoundError, ForbiddenError, ValidationError) as e:
        raise _map_err(e)
    return {
        "offer": offer.to_dict(),
        "availability": row.to_dict(total_chunks=offer.total_chunks),
    }


@router.post("/group-file-offers/{offer_id}/reject")
async def reject_offer(
    offer_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        offer, row = await GroupFileService.reject_offer(db, offer_id, user_id)
    except (NotFoundError, ForbiddenError, ValidationError) as e:
        raise _map_err(e)
    return {
        "offer": offer.to_dict(),
        "availability": row.to_dict(total_chunks=offer.total_chunks),
    }


# ── Chunk progress / swarm ────────────────────────────────────────

@router.post("/group-file-offers/{offer_id}/chunks/{chunk_index}")
async def report_chunk_received(
    offer_id: str,
    chunk_index: int,
    body: ReportChunkBody | None = None,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        row, flipped, completed = await GroupFileService.report_chunk_received(
            db,
            offer_id,
            user_id,
            chunk_index,
            chunk_bytes=(body.chunk_bytes if body else None),
        )
        offer = await GroupFileService.get_offer(db, offer_id)
    except (NotFoundError, ForbiddenError, ValidationError) as e:
        raise _map_err(e)
    return {
        "flipped": flipped,
        "became_complete": completed,
        "availability": row.to_dict(total_chunks=offer.total_chunks),
    }


@router.get("/group-file-offers/{offer_id}/chunks/{chunk_index}/peers")
async def chunk_peers(
    offer_id: str,
    chunk_index: int,
    limit: int = Query(default=32, ge=1, le=256),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        peers = await GroupFileService.get_chunk_peers(
            db, offer_id, chunk_index,
            exclude_user_id=user_id, limit=limit,
        )
    except (NotFoundError, ForbiddenError, ValidationError) as e:
        raise _map_err(e)
    return {"offer_id": offer_id, "chunk_index": chunk_index, "peers": peers}


# ── Cancel ────────────────────────────────────────────────────────

@router.delete("/group-file-offers/{offer_id}")
async def cancel_offer(
    offer_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        offer = await GroupFileService.get_offer(db, offer_id)
    except NotFoundError as e:
        raise _map_err(e)

    # Sender or channel admin can cancel.
    is_sender = offer.sender_id == user_id
    is_admin = False
    if not is_sender:
        channel = await ChannelService.get_channel(db, offer.channel_id)
        is_admin = any(
            m.user_id == user_id and (m.role or "").lower() in ("admin", "owner")
            for m in (channel.members or [])
        )
    if not (is_sender or is_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "only sender or channel admin can cancel")

    try:
        offer = await GroupFileService.cancel_offer(db, offer_id, user_id)
    except (NotFoundError, ForbiddenError, ValidationError) as e:
        raise _map_err(e)
    return offer.to_dict()


# ── Sweep (admin / cron) ──────────────────────────────────────────

@router.post("/group-file-offers/_sweep-expired")
async def sweep_expired(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    # Minimal gate — we don't want random users tripping the sweep.
    # This path is mostly for the scheduled task; callers without admin
    # bit get 403.
    from app.services.user_service import UserService
    user = await UserService.get_user(db, user_id)
    role = (getattr(user, "role", None) or "").lower()
    if role not in ("admin", "owner"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    expired = await GroupFileService.sweep_expired(db)
    stale = await GroupFileService.cleanup_stale_recipients(db)
    return {"expired_offers": expired, "abandoned_recipients": stale}


__all__ = ["router"]
