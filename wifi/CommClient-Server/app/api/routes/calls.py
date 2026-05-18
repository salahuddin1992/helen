"""
Call log REST endpoints — retrieve call history.

Also exposes lightweight discovery endpoints used by the UI to render
"join existing call" affordances without spinning up a full v2_call_join_group
event first:
  * GET /api/channels/{channel_id}/active-call
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.call_log import CallLog
from app.models.channel import ChannelMember
from app.schemas.call import CallLogListResponse, CallLogResponse
from app.services.call_service import call_service
from app.services.channel_service import ChannelService

logger = get_logger(__name__)

router = APIRouter(prefix="/calls", tags=["calls"])

# Secondary router for /channels/{id}/active-call so it groups under
# /channels in the OpenAPI spec without polluting the channels.py file.
channel_call_router = APIRouter(prefix="/channels", tags=["calls"])


@router.get("", response_model=CallLogListResponse)
async def list_call_logs(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get call history for the current user (calls they participated in)."""
    # Get user's channels
    ch_result = await db.execute(
        select(ChannelMember.channel_id).where(ChannelMember.user_id == user_id)
    )
    channel_ids = [r[0] for r in ch_result.all()]

    # Calls in user's channels OR initiated by user
    from sqlalchemy import or_
    where_clause = or_(
        CallLog.channel_id.in_(channel_ids) if channel_ids else False,
        CallLog.initiator_id == user_id,
    )

    total_q = select(func.count()).select_from(CallLog).where(where_clause)
    total = (await db.execute(total_q)).scalar() or 0

    result = await db.execute(
        select(CallLog)
        .where(where_clause)
        .order_by(CallLog.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    calls = result.scalars().all()

    return CallLogListResponse(
        calls=[CallLogResponse.model_validate(c) for c in calls],
        total=total,
    )


@router.delete("/{call_id}", status_code=204)
async def delete_call_log(
    call_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single call-log entry.

    Authorization: user must have been a participant — either the
    initiator OR a member of the channel the call ran in. We don't
    cascade-delete other users' references to the same call; this is
    a per-user "remove from my history" operation. (For the canonical
    case where every participant has the same row, that means a single
    DELETE wipes it for everyone — acceptable for a LAN-scoped log
    where everyone trusts everyone.)
    """
    result = await db.execute(select(CallLog).where(CallLog.id == call_id))
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    if call.initiator_id != user_id:
        member_q = await db.execute(
            select(ChannelMember).where(
                ChannelMember.channel_id == call.channel_id,
                ChannelMember.user_id == user_id,
            )
        )
        if not member_q.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Not a participant of this call")

    await db.delete(call)
    await db.commit()


@router.post("/{call_id}/leave-on-close", status_code=204)
async def leave_call_on_close(
    call_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Best-effort hangup/leave when the renderer is being torn down.

    The desktop client fires this via ``navigator.sendBeacon`` on
    ``beforeunload`` so the server learns about the departure within
    one HTTP round-trip instead of waiting for the 30-second orphan
    sweep to notice the dead Socket.IO connection. Routes to
    ``leave_call`` for group routing and ``hangup`` for p2p.

    Idempotent — unknown call_ids return 204 silently. Errors are
    swallowed because the page is closing and the client can't read
    the response anyway; the orphan sweep is the authoritative
    fallback.
    """
    try:
        # Resolve the in-memory call so we can pick the right path.
        # Reach into _active_calls directly — the public _get_call()
        # raises if the call is already cleaned up, and during page
        # close we want a silent no-op in that case.
        call = call_service._active_calls.get(call_id)  # type: ignore[attr-defined]
        if not call:
            return
        if user_id not in call.participants:
            return
        if call.routing == "p2p":
            await call_service.hangup(call_id, user_id)
        else:
            await call_service.leave_call(call_id, user_id)
    except Exception as exc:
        logger.warning(
            "leave_on_close_failed",
            call_id=call_id, user_id=user_id, error=str(exc),
        )


@router.delete("", status_code=204)
async def clear_call_history(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Clear all call-log rows visible to the current user.

    Same scope as `GET /api/calls`: calls in any channel the user is a
    member of, plus any call they initiated. We wipe every matching
    row in one statement — no bulk-soft-delete column on this model.
    """
    from sqlalchemy import delete as sa_delete, or_
    ch_result = await db.execute(
        select(ChannelMember.channel_id).where(ChannelMember.user_id == user_id)
    )
    channel_ids = [r[0] for r in ch_result.all()]

    where_clause = or_(
        CallLog.channel_id.in_(channel_ids) if channel_ids else False,
        CallLog.initiator_id == user_id,
    )
    await db.execute(sa_delete(CallLog).where(where_clause))
    await db.commit()


# ── Active call discovery (for "Join Existing Call" UX) ──────────────
#
# The UI in QuickCallSheet.tsx / GroupActionHub.tsx needs to know whether
# a group call is currently live in a given channel BEFORE the user
# commits to v2_call_join_group. Without this, the user can't see "5
# people in call — Join" until they actually start a join attempt.
#
# This endpoint is intentionally cheap: it reads in-memory ActiveCall
# state from call_service and falls back to the active_calls DB table
# if state was rehydrated from a sibling-server's persistence.

@channel_call_router.get("/{channel_id}/active-call")
async def get_channel_active_call(
    channel_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Return the active group call for a channel, or null if none.

    Membership is required — non-members get 403 even for read access.
    Cross-server: if the call lives on a sibling Helen server, the local
    call_service won't have it; the federation-backed lookup happens
    via call_state_persistence (which mirrors active_calls into the
    shared / replicated DB on each lifecycle write).

    Response shape (matches QuickCallSheet's CallParticipantPreview):
        {
          "active_call": null
        }
        — OR —
        {
          "active_call": {
            "call_id": "<hex>",
            "call_type": "audio" | "video",
            "routing": "p2p" | "mesh" | "sfu" | "hybrid",
            "status": "ringing" | "active",
            "started_at": "<iso>" | null,
            "participant_count": <int>,
            "participants": [
              {
                "user_id": "<uuid>",
                "muted": <bool>,
                "video_off": <bool>,
                "sharing_screen": <bool>,
                "on_hold": <bool>
              }, …
            ],
            "host_id": "<uuid>"
          }
        }
    """
    # Membership gate — same level of trust as v2_call_join_group itself.
    if not await ChannelService.is_member(db, channel_id, user_id):
        from app.core.audit import audit_permission_denied
        audit_permission_denied(user_id, f"channel:{channel_id}", "active_call_read")
        raise HTTPException(status_code=403, detail="Not a member of this channel")

    # 1. Fast path — call lives in this server's memory.
    call = call_service.get_call_by_channel(channel_id)
    if call:
        return {
            "active_call": {
                "call_id": call.call_id,
                "call_type": call.call_type,
                "routing": call.routing,
                "status": call.status,
                "started_at": call.started_at.isoformat() if call.started_at else None,
                "participant_count": len(call.participants),
                "participants": [
                    {
                        "user_id": uid,
                        "muted": p.get("muted", False),
                        "video_off": p.get("video_off", False),
                        "sharing_screen": p.get("sharing_screen", False),
                        "on_hold": uid in call.on_hold_users,
                    }
                    for uid, p in call.participants.items()
                ],
                "host_id": call.initiator_id,
            }
        }

    # 2. DB fallback — picks up calls hosted on sibling servers that
    #    persisted via call_state_persistence. Best-effort; if the
    #    sibling server died mid-call this row will be reaped by the
    #    DB orphan sweep eventually.
    try:
        from app.services.call_state_persistence import call_state_persistence
        row = await call_state_persistence.get_active_by_channel(channel_id)
    except Exception as exc:
        logger.debug("active_call_db_lookup_failed", channel_id=channel_id, error=str(exc))
        row = None

    if row:
        return {"active_call": row}

    return {"active_call": None}


# ── Federated ICE / relay chain ───────────────────────────
#
# For a call that needs to traverse multiple Helen servers (e.g. two
# clients in different LANs bridged by 1–N intermediate servers), the
# client POSTs here with the destination server_id and the final UDP
# target on that server. We build a transparent UDP relay chain and
# return the single entry `(host, port)` the client should dial. The
# chain's `hops` list is echoed back so the client (or the calling
# server) can tear it down via DELETE when the call ends.


@router.post("/federated/relay/chain", status_code=201)
async def open_federated_relay_chain(
    body: dict = Body(...),
    user_id: str = Depends(get_current_user_id),
):
    """Open a multi-hop UDP relay chain to a far-side server.

    Body:
      {
        "target_server_id": "...",      # dst server as advertised on federation
        "target_host": "<ipv4>",        # final UDP dest on dst server's LAN
        "target_port": <int>,           # final UDP port
        "idle_ttl_seconds": <optional>  # default 180
      }

    Returns:
      {
        "entry_host": "...",
        "entry_port": <int>,
        "hops": [
            {"server_id": "...", "relay_id": "...",
             "ingress_host": "...", "ingress_port": ...},
            ...
        ],
        "final_next_hop": {"host": "...", "port": ...}
      }
    """
    from app.core.config import get_settings
    settings = get_settings()
    if not settings.FEDERATION_ENABLED or not settings.FEDERATION_SECRET:
        raise HTTPException(status_code=503, detail="federation disabled")

    target_server_id = body.get("target_server_id")
    target_host = body.get("target_host")
    target_port = body.get("target_port")
    idle_ttl = float(body.get("idle_ttl_seconds") or 180.0)
    if not target_server_id or not target_host or not isinstance(target_port, int):
        raise HTTPException(
            status_code=400,
            detail="missing target_server_id/target_host/target_port",
        )
    if not (1 <= target_port <= 65535):
        raise HTTPException(status_code=400, detail="bad target_port")

    from app.services.relay_path import build_chain
    chain = await build_chain(
        dst_server_id=target_server_id,
        dst_host=target_host,
        dst_port=target_port,
        idle_ttl=idle_ttl,
    )
    if chain is None:
        raise HTTPException(status_code=502, detail="no relay path found")
    return chain.to_dict()


@router.delete("/federated/relay/chain")
async def close_federated_relay_chain(
    body: dict = Body(...),
    user_id: str = Depends(get_current_user_id),
):
    """Tear down a relay chain.

    Body: { "hops": [{"server_id": "...", "relay_id": "..."}, ...] }
    """
    hops = body.get("hops") or []
    if not isinstance(hops, list) or not hops:
        raise HTTPException(status_code=400, detail="missing hops[]")

    from app.services.peer_registry import peer_registry
    from app.services.relay_path import _known_peers, _release_on_peer
    import asyncio as _asyncio

    async def _teardown(hop: dict):
        sid = hop.get("server_id")
        rid = hop.get("relay_id")
        if not sid or not rid:
            return
        peer = _known_peers.get(sid) or await peer_registry.get(sid)
        if peer is None:
            return
        await _release_on_peer(peer, rid)

    await _asyncio.gather(*[_teardown(h) for h in hops], return_exceptions=True)
    return {"released": len(hops)}


@router.get("/federated/ice")
async def federated_ice_config(
    target_server_id: str = Query(...),
    target_host: str = Query(...),
    target_port: int = Query(..., ge=1, le=65535),
    user_id: str = Depends(get_current_user_id),
):
    """Convenience: open a chain and return an ICE-candidate-shaped blob.

    Useful for naive clients that don't understand the chain structure;
    they get a single UDP `(host, port)` they can feed directly into
    their existing ICE candidate list.
    """
    from app.core.config import get_settings
    settings = get_settings()
    if not settings.FEDERATION_ENABLED or not settings.FEDERATION_SECRET:
        raise HTTPException(status_code=503, detail="federation disabled")

    from app.services.relay_path import build_chain
    chain = await build_chain(
        dst_server_id=target_server_id,
        dst_host=target_host,
        dst_port=target_port,
    )
    if chain is None:
        raise HTTPException(status_code=502, detail="no relay path found")
    return {
        "ice_server": {
            "urls": f"relay:udp:{chain.entry_host}:{chain.entry_port}",
            "host": chain.entry_host,
            "port": chain.entry_port,
        },
        "chain": chain.to_dict(),
    }
