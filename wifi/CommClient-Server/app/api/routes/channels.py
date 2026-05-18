"""
Channel / room REST endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import ForbiddenError
from app.schemas.channel import (
    AddMemberRequest,
    ChannelArchiveRequest,
    ChannelCreate,
    ChannelListResponse,
    ChannelMemberInfo,
    ChannelMemberPrefsResponse,
    ChannelMuteRequest,
    ChannelPinRequest,
    ChannelReadRequest,
    ChannelResponse,
    ChannelUpdate,
)
from app.services.channel_service import ChannelService

router = APIRouter(prefix="/channels", tags=["channels"])


def _channel_to_response(channel) -> ChannelResponse:
    members = []
    for m in channel.members:
        if m.user is None:
            continue  # Skip orphaned members (user deleted or not loaded)
        members.append(ChannelMemberInfo(
            user_id=m.user_id,
            username=m.user.username,
            display_name=m.user.display_name,
            avatar_url=m.user.avatar_url,
            status=m.user.status,
            role=m.role,
            joined_at=m.joined_at,
        ))
    return ChannelResponse(
        id=channel.id,
        type=channel.type,
        name=channel.name,
        description=channel.description,
        avatar_url=channel.avatar_url,
        created_by=channel.created_by,
        is_active=channel.is_active,
        members=members,
        member_count=len(members),
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


@router.post("", response_model=ChannelResponse, status_code=201)
async def create_channel(
    body: ChannelCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    # Admission gate + placement selection in one atomic decision.
    chosen_node_id = None
    try:
        from app.services.control_plane import ControlPlane
        from app.services.placement import place, RoomRequest
        from app.services.node_registry import get_registry

        cp = ControlPlane.instance()
        ctype = (body.type or "").lower()
        cp_kind = "call" if ctype in ("voice", "video") else "room"
        placement_kind = {
            "dm": "chat", "group": "chat", "channel": "chat",
            "voice": "audio", "video": "video", "broadcast": "broadcast",
        }.get(ctype, "chat")

        allowed, reason = cp.is_admission_allowed(cp_kind)
        if not allowed:
            raise HTTPException(
                status_code=503,
                detail={"error": "admission_refused", "reason": reason,
                        "phase": cp.status().get("global", {}).get("phase")},
            )

        # Run placement — strongest+healthiest node wins.
        decision = place(RoomRequest(
            kind=placement_kind,
            participants_est=len(body.member_ids or []) + 1,
            priority="normal",
            creator_node_id=get_registry().self_node_id,
        ))
        if not decision.assigned:
            raise HTTPException(
                status_code=503,
                detail={"error": "placement_refused",
                        "reason": decision.refused_reason,
                        "alternatives": decision.alternatives},
            )
        chosen_node_id = decision.node_id
    except HTTPException:
        raise
    except Exception:
        pass  # control plane optional — never block creation on its failure
    channel = await ChannelService.create_channel(
        db,
        creator_id=user_id,
        channel_type=body.type,
        name=body.name,
        description=body.description,
        member_ids=body.member_ids,
    )
    # Register the new channel with the control plane so per-room
    # decisions can target it. Non-fatal on failure.
    try:
        from app.services.control_plane import ControlPlane
        kind_map = {"dm": "chat", "group": "chat", "channel": "chat"}
        cp_kind = kind_map.get((body.type or "").lower(), "chat")
        ControlPlane.instance().register_room(
            str(channel.id), kind=cp_kind,
            participants=len(body.member_ids or []) + 1,
        )
    except Exception:
        pass
    return _channel_to_response(channel)


@router.get("", response_model=ChannelListResponse)
async def list_channels(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    channels = await ChannelService.list_user_channels(db, user_id)
    return ChannelListResponse(
        channels=[_channel_to_response(ch) for ch in channels],
        total=len(channels),
    )


@router.get("/{channel_id}", response_model=ChannelResponse)
async def get_channel(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    channel = await ChannelService.get_channel(db, channel_id)
    if not await ChannelService.is_member(db, channel_id, user_id):
        raise ForbiddenError("You are not a member of this channel")
    return _channel_to_response(channel)


@router.patch("/{channel_id}", response_model=ChannelResponse)
async def update_channel(
    channel_id: str,
    body: ChannelUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    channel = await ChannelService.update_channel(
        db, channel_id, user_id, **body.model_dump(exclude_unset=True),
    )
    return _channel_to_response(channel)


@router.post("/{channel_id}/members", status_code=201)
async def add_member(
    channel_id: str,
    body: AddMemberRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await ChannelService.add_member(db, channel_id, user_id, body.user_id, body.role)
    return {"status": "member_added"}


@router.delete("/{channel_id}/members/{member_id}", status_code=204, response_class=Response)
async def remove_member(
    channel_id: str,
    member_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await ChannelService.remove_member(db, channel_id, user_id, member_id)
    return Response(status_code=204)


@router.delete("/{channel_id}", status_code=204, response_class=Response)
async def delete_channel(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a channel (group / DM / channel).

    Authorization:
      • The channel's `created_by` user (the room owner)
      • A site-wide ``admin`` role user
      • For DMs: either of the two participants

    On delete, every ChannelMember row is wiped — effectively kicking
    every participant out — and the channel itself is removed. Messages
    cascade-delete via the FK relationship in the model. We broadcast a
    ``channel:deleted`` socket event to all former members so their UIs
    drop the channel from the list immediately.
    """
    from app.models.channel import Channel as _Channel, ChannelMember as _ChannelMember
    from app.models.user import User as _User
    from sqlalchemy import delete as _sa_delete
    from app.socket.server import sio
    from fastapi import HTTPException, status

    res = await db.execute(select(_Channel).where(_Channel.id == channel_id))
    channel = res.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Authorization. DMs allow either participant; everything else
    # requires creator or site admin.
    is_creator = channel.created_by == user_id
    is_dm_member = False
    if (channel.type or "").lower() == "dm":
        m_res = await db.execute(
            select(_ChannelMember).where(
                _ChannelMember.channel_id == channel_id,
                _ChannelMember.user_id == user_id,
            )
        )
        is_dm_member = m_res.scalar_one_or_none() is not None

    is_site_admin = False
    if not is_creator and not is_dm_member:
        u_res = await db.execute(select(_User).where(_User.id == user_id))
        u = u_res.scalar_one_or_none()
        is_site_admin = u is not None and u.role == "admin"

    if not (is_creator or is_dm_member or is_site_admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the channel creator or a server admin can delete this channel",
        )

    # Snapshot member list BEFORE deletion so we can fan out the
    # `channel:deleted` event to every former member's socket(s).
    m_q = await db.execute(
        select(_ChannelMember.user_id).where(_ChannelMember.channel_id == channel_id)
    )
    member_ids = [r[0] for r in m_q.all()]

    # Delete members + channel. Messages cascade via FK ondelete=CASCADE.
    await db.execute(_sa_delete(_ChannelMember).where(_ChannelMember.channel_id == channel_id))
    await db.delete(channel)
    await db.commit()

    # Broadcast — every member's connected sockets drop the channel.
    try:
        from app.services.presence_service import presence_service
        for mid in member_ids:
            sids = presence_service.get_sids(mid)
            for sid in sids:
                await sio.emit(
                    "channel:deleted",
                    {"channel_id": channel_id, "deleted_by": user_id},
                    to=sid,
                )
    except Exception:
        # Broadcast is best-effort; clients re-fetch on next reconnect.
        pass

    return Response(status_code=204)


# ── Per-user channel preferences (archive / mute / pin / read) ───────────

def _member_prefs_response(m) -> ChannelMemberPrefsResponse:
    return ChannelMemberPrefsResponse(
        channel_id=m.channel_id,
        user_id=m.user_id,
        is_archived=bool(m.is_archived),
        is_pinned=bool(m.is_pinned),
        is_muted=bool(m.is_muted),
        mute_until=m.mute_until,
        last_read_at=m.last_read_at,
        last_read_message_id=m.last_read_message_id,
    )


@router.put("/{channel_id}/archive", response_model=ChannelMemberPrefsResponse)
async def archive_channel(
    channel_id: str,
    body: ChannelArchiveRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Toggle archive state for the current user."""
    member = await ChannelService.set_archived(db, channel_id, user_id, body.archived)
    return _member_prefs_response(member)


@router.put("/{channel_id}/pin", response_model=ChannelMemberPrefsResponse)
async def pin_channel(
    channel_id: str,
    body: ChannelPinRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Pin/unpin a channel to the top of the user's list."""
    member = await ChannelService.set_pinned(db, channel_id, user_id, body.pinned)
    return _member_prefs_response(member)


@router.put("/{channel_id}/mute", response_model=ChannelMemberPrefsResponse)
async def mute_channel(
    channel_id: str,
    body: ChannelMuteRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Mute or unmute notifications for a channel.
    Pass mute_until to auto-unmute at a future time (otherwise mute is indefinite).
    """
    member = await ChannelService.set_muted(
        db, channel_id, user_id, body.muted, mute_until=body.mute_until,
    )
    return _member_prefs_response(member)


@router.put("/{channel_id}/read", response_model=ChannelMemberPrefsResponse)
async def mark_channel_read(
    channel_id: str,
    body: ChannelReadRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Update the user's last-read pointer for a channel."""
    member = await ChannelService.update_last_read(
        db, channel_id, user_id, message_id=body.message_id,
    )
    return _member_prefs_response(member)
