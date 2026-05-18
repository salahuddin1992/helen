"""
Call signaling socket event handlers.
Handles 1-to-1 (P2P) and group call lifecycle + WebRTC signaling relay.
"""

from __future__ import annotations

import time

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.services.call_service import call_service
from app.services.channel_service import ChannelService
from app.services.ice_config_service import build_ice_config, build_ice_servers
from app.services.presence_service import presence_service
from app.socket.server import emit_to_user, get_user_id, sio

logger = get_logger(__name__)


def _local_authz_seed(call_id: str, participants):
    """Seed the call-signal authz shadow with our own server_id as
    origin so cross-server lifecycle RPCs (accept/reject/hangup/leave)
    that arrive on a sibling server know where to forward."""
    try:
        from app.services.call_signal_authz import call_signal_authz
        from app.services.discovery_service import get_server_id
        call_signal_authz.seed(call_id, participants, origin_server_id=get_server_id())
    except Exception as e:
        logger.debug("local_authz_seed_failed", error=str(e))


def _schedule_missed_call_timeout(call_id: str, initiator_id: str, timeout_seconds: float = 30.0) -> None:
    """Fire-and-forget timer that ends a still-ringing call after N
    seconds. Without this, a callee who never picks up leaves the
    caller's UI stuck on "ringing" forever and the ActiveCall sits in
    memory until the heartbeat cleanup eventually reaps it.

    Behaviour:
      * If the call is no longer ringing when the timer fires (already
        accepted, rejected, hung up, or ended), the timer is a no-op.
      * Otherwise we mark it ended via call_service.hangup, emit
        call:missed / call_missed to the initiator, persist the log,
        and clear the authz shadow.
    """
    import asyncio as _asyncio_miss

    async def _go():
        try:
            await _asyncio_miss.sleep(timeout_seconds)
            call = call_service.get_call(call_id)
            if not call or call.status != "ringing":
                return  # accepted/rejected/ended — nothing to do
            logger.info("call_missed_timeout_fired",
                        call_id=call_id, initiator_id=initiator_id,
                        elapsed=timeout_seconds)
            try:
                ended = await call_service.hangup(call_id, initiator_id)
            except Exception as e:
                logger.warning("missed_call_hangup_failed",
                               call_id=call_id, error=str(e))
                return

            # Notify the initiator (and any participants — group ring case).
            for pid in list(ended.participants.keys()) or [initiator_id]:
                try:
                    await emit_to_user("call:missed", {
                        "call_id": call_id,
                        "reason": "no_answer",
                    }, pid)
                    await emit_to_user("call_missed", {
                        "call_id": call_id,
                        "reason": "no_answer",
                    }, pid)
                    await presence_service.set_status(pid, "online")
                except Exception:
                    pass

            try:
                from app.services.call_signal_authz import call_signal_authz
                call_signal_authz.clear(call_id)
            except Exception:
                pass

            try:
                async with async_session_factory() as db:
                    await call_service.persist_call_log(db, ended)
            except Exception as e:
                logger.warning("missed_call_persist_failed", error=str(e))
        except _asyncio_miss.CancelledError:
            return
        except Exception as e:
            logger.warning("missed_call_timer_unhandled", error=str(e))

    task = _asyncio_miss.create_task(_go())
    task.set_name(f"missed_call:{call_id}")
    try:
        call_service._bg_tasks.add(task)
        task.add_done_callback(call_service._bg_tasks.discard)
    except Exception:
        pass


async def _maybe_forward_to_origin(
    call_id: str,
    rpc: str,
    user_id: str,
    extra: dict | None = None,
) -> tuple[bool, dict | None]:
    """If the ActiveCall lives on a sibling Helen server, forward the
    RPC there and return (True, response_to_send_to_client). Otherwise
    return (False, None) so the caller runs the local path.

    This is the cross-server bridge that lets a callee on server-2
    accept/reject/leave/hangup/reinvite a call whose authoritative
    state sits on server-1.
    """
    if call_service.get_call(call_id) is not None:
        return False, None  # local path

    from app.services.call_signal_authz import call_signal_authz
    origin = call_signal_authz.origin_of(call_id)
    if not origin:
        return True, {"error": "Call not found"}

    # Never forward to ourselves (would loop). If our shadow says we're
    # the origin but call_service has no record, it really *is* gone.
    from app.services.discovery_service import get_server_id
    if origin == get_server_id():
        return True, {"error": "Call not found"}

    from app.services.federation_service import federation_service

    # Server-side retry with exponential backoff. Without this, a
    # transient connection blip on the federation HTTP transport
    # surfaces to the client as "origin_unreachable" and the user has
    # to re-click. Three retries spaced 100/300/700 ms cover the
    # common hiccup window without holding the socket handler open
    # for too long. ``hangup`` / ``leave`` are still safe to retry
    # because the origin handles them idempotently.
    import asyncio as _asyncio_fed
    backoffs = (0.0, 0.1, 0.3, 0.7)
    resp = None
    for delay in backoffs:
        if delay > 0:
            await _asyncio_fed.sleep(delay)
        resp = await federation_service.forward_call_rpc(
            origin, rpc, call_id, user_id, extra=extra,
        )
        if resp is not None:
            break
    if resp is None:
        logger.warning(
            "federation_rpc_origin_unreachable_after_retries",
            origin=origin, rpc=rpc, call_id=call_id,
        )
        return True, {"error": "origin_unreachable"}
    if not resp.get("ok"):
        return True, {"error": resp.get("error") or "rpc_failed"}
    result = resp.get("result") or {}
    # Make sure caller-side state mirrors origin: clear or mark left.
    if rpc in ("hangup", "reject"):
        call_signal_authz.clear(call_id)
    elif rpc == "leave":
        call_signal_authz.remove_participant(call_id, user_id)
    return True, result


# ── 1-to-1 Call Events ──────────────────────────────────

@sio.event
async def call_initiate(sid: str, data: dict):
    """
    Initiate a 1-to-1 call.
    data: { callee_id: str, media_type: "audio" | "video" }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    callee_id = data.get("callee_id")
    media_type = data.get("media_type", "audio")

    if not callee_id:
        return {"error": "callee_id is required"}

    # ── Block enforcement ──
    try:
        from app.services.user_service import UserService as _US
        async with async_session_factory() as db:
            blocked, blocker = await _US.is_blocked_either_way(db, user_id, callee_id)
        if blocked:
            from app.core.audit import audit_call_signal_unauthorized
            audit_call_signal_unauthorized(user_id, callee_id, "call_initiate_blocked")
            if blocker == user_id:
                return {"error": "You have blocked this user. Unblock them to call."}
            return {"error": "You cannot call this user."}
    except Exception as e:
        logger.warning("call_block_check_failed", error=str(e))

    try:
        call = await call_service.initiate_call(
            initiator_id=user_id,
            call_type=media_type,
            routing="p2p",
        )

        # Seed signal-authz shadow so signaling works in BOTH directions
        # (local→remote callee and remote→local) once they're connected.
        # Origin = us; cross-server callees forward accept/reject back here.
        _local_authz_seed(call.call_id, [user_id, callee_id])

        # Arm the no-answer timer — if no accept lands within 30s the
        # call is auto-marked missed and the caller is freed.
        _schedule_missed_call_timeout(call.call_id, user_id)

        # Notify callee — emit_to_user falls back to federation if the
        # callee lives on a sibling Helen server in the cluster.
        await emit_to_user("call:incoming", {
            "call_id": call.call_id,
            "caller_id": user_id,
            "media_type": media_type,
        }, callee_id)

        # Update caller's presence
        await presence_service.set_status(user_id, "in_call")
        await sio.emit("presence:user_status", {
            "user_id": user_id,
            "status": "in_call",
        })

        return {"call_id": call.call_id}

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def call_accept(sid: str, data: dict):
    """
    Accept an incoming call.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    try:
        call = await call_service.accept_call(call_id, user_id)

        # Refresh authz shadow with the now-confirmed participant set.
        _local_authz_seed(call_id, list(call.participants.keys()))

        # Notify initiator that call was accepted
        await emit_to_user("call:accepted", {
            "call_id": call_id,
            "callee_id": user_id,
        }, call.initiator_id)

        # Update presence
        await presence_service.set_status(user_id, "in_call")
        await sio.emit("presence:user_status", {
            "user_id": user_id,
            "status": "in_call",
        })

        # Tell both peers they can start signaling.
        # Each peer gets its own ephemeral TURN credentials.
        for pid in call.participants:
            try:
                ice_cfg = build_ice_config(pid)
            except Exception as e:
                logger.warning("ice_config_build_failed", user_id=pid, error=str(e))
                ice_cfg = {"ice_servers": [], "ice_transport_policy": "all"}
            await emit_to_user("call:peer_ready", {
                "call_id": call_id,
                "participants": list(call.participants.keys()),
                "ice_servers": ice_cfg["ice_servers"],
                "ice_transport_policy": ice_cfg["ice_transport_policy"],
                "ice_ttl_seconds": ice_cfg.get("ttl_seconds"),
            }, pid)

        return {"status": "accepted"}

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def call_reject(sid: str, data: dict):
    """
    Reject an incoming call.
    data: { call_id: str, reason?: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    reason = data.get("reason", "rejected")

    if not call_id:
        return {"error": "call_id is required"}

    try:
        call = await call_service.reject_call(call_id, user_id)

        # Notify initiator
        await emit_to_user("call:rejected", {
            "call_id": call_id,
            "user_id": user_id,
            "reason": reason,
        }, call.initiator_id)

        # Reset initiator presence
        await presence_service.set_status(call.initiator_id, "online")
        await sio.emit("presence:user_status", {
            "user_id": call.initiator_id,
            "status": "online",
        })

        # Persist call log + clear cross-server signal authz
        from app.services.call_signal_authz import call_signal_authz
        call_signal_authz.clear(call_id)
        async with async_session_factory() as db:
            await call_service.persist_call_log(db, call)

        return {"status": "rejected"}

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def call_hangup(sid: str, data: dict):
    """
    End a call.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    try:
        call = call_service.get_call(call_id)
        if not call:
            return {"error": "Call not found"}

        # Notify all participants before ending
        for pid in list(call.participants.keys()):
            if pid != user_id:
                await emit_to_user("call:ended", {
                    "call_id": call_id,
                    "ended_by": user_id,
                    "reason": "hangup",
                }, pid)

            # Reset presence
            await presence_service.set_status(pid, "online")
            await sio.emit("presence:user_status", {
                "user_id": pid,
                "status": "online",
            })

        ended_call = await call_service.hangup(call_id, user_id)

        # Persist call log + clear cross-server signal authz
        from app.services.call_signal_authz import call_signal_authz
        call_signal_authz.clear(call_id)
        async with async_session_factory() as db:
            await call_service.persist_call_log(db, ended_call)

        return {"status": "ended"}

    except ValueError as e:
        return {"error": str(e)}


# ── Group Call Events ────────────────────────────────────

@sio.event
async def call_join_group(sid: str, data: dict):
    """
    [DEPRECATED] v1 group join — superseded by ``v2_call_join_group``.

    The legacy handler hard-coded ``routing="sfu"`` while the v2 client
    expects mesh-with-promotion. Letting both coexist created a
    routing split that produced two parallel calls on the same
    channel depending on which store the caller used.

    This entry-point now logs a deprecation warning, refuses, and
    returns an error so any caller still on v1 fails fast and can be
    migrated. Remove the function body entirely once telemetry shows
    zero v1 traffic.
    """
    user_id = await get_user_id(sid)
    logger.warning(
        "deprecated_v1_handler_called",
        handler="call_join_group",
        user_id=user_id,
        channel_id=(data or {}).get("channel_id"),
    )
    return {
        "error": "deprecated_v1_handler",
        "detail": "Use v2_call_join_group. Update your client.",
    }


# Original implementation kept below for reference until removal.
async def _legacy_call_join_group_impl_unused(sid: str, data: dict):  # pragma: no cover
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    channel_id = data.get("channel_id")
    media_type = data.get("media_type", "audio")

    if not channel_id:
        return {"error": "channel_id is required"}

    # SECURITY: Verify channel membership before allowing group call join
    try:
        async with async_session_factory() as db:
            if not await ChannelService.is_member(db, channel_id, user_id):
                from app.core.audit import audit_permission_denied
                audit_permission_denied(user_id, f"channel:{channel_id}", "call_join_group")
                logger.warning("call_join_group_unauthorized", user_id=user_id, channel_id=channel_id)
                return {"error": "Not a member of this channel"}
    except Exception as e:
        logger.error("call_join_group_membership_check_error", error=str(e))
        return {"error": "Failed to verify channel membership"}

    try:
        # Check if there's an existing group call for this channel
        call = call_service.get_call_by_channel(channel_id)

        if call:
            # Join existing call
            call = await call_service.join_group_call(call.call_id, user_id)
        else:
            # Create new group call
            call = await call_service.initiate_call(
                initiator_id=user_id,
                call_type=media_type,
                routing="sfu",
                channel_id=channel_id,
            )

        # Notify all channel members about the group call
        async with async_session_factory() as db:
            channel = await ChannelService.get_channel(db, channel_id)
            member_ids = [m.user_id for m in channel.members if m.user_id != user_id]

        # Seed authz so cross-server members can signal once they accept.
        _local_authz_seed(call.call_id, list(call.participants.keys()))

        for member_id in member_ids:
            try:
                m_ice = build_ice_config(member_id)
            except Exception as e:
                logger.warning("ice_config_build_failed", user_id=member_id, error=str(e))
                m_ice = {"ice_servers": [], "ice_transport_policy": "all"}
            if member_id in call.participants:
                # Already in call — notify about new participant
                await emit_to_user("call:peer_joined", {
                    "call_id": call.call_id,
                    "user_id": user_id,
                    "participants": list(call.participants.keys()),
                    "ice_servers": m_ice["ice_servers"],
                    "ice_transport_policy": m_ice["ice_transport_policy"],
                    "ice_ttl_seconds": m_ice.get("ttl_seconds"),
                }, member_id)
            else:
                # Not in call — send ring notification
                await emit_to_user("call:group_ringing", {
                    "call_id": call.call_id,
                    "channel_id": channel_id,
                    "media_type": media_type,
                    "participants": list(call.participants.keys()),
                    "ice_servers": m_ice["ice_servers"],
                    "ice_transport_policy": m_ice["ice_transport_policy"],
                    "ice_ttl_seconds": m_ice.get("ttl_seconds"),
                }, member_id)

        await presence_service.set_status(user_id, "in_call")
        await sio.emit("presence:user_status", {"user_id": user_id, "status": "in_call"})

        try:
            joiner_ice = build_ice_config(user_id)
        except Exception:
            joiner_ice = {"ice_servers": [], "ice_transport_policy": "all"}

        return {
            "call_id": call.call_id,
            "participants": list(call.participants.keys()),
            "ice_servers": joiner_ice["ice_servers"],
            "ice_transport_policy": joiner_ice["ice_transport_policy"],
            "ice_ttl_seconds": joiner_ice.get("ttl_seconds"),
        }

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def call_leave_group(sid: str, data: dict):
    """
    Leave a group call.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    try:
        call = call_service.get_call(call_id)
        if not call:
            return {"error": "Call not found"}

        remaining_participants = list(call.participants.keys())
        call = await call_service.leave_call(call_id, user_id)

        # Notify remaining participants
        for pid in remaining_participants:
            if pid != user_id:
                await emit_to_user("call:peer_left", {
                    "call_id": call_id,
                    "user_id": user_id,
                    "participants": list(call.participants.keys()) if call.status != "ended" else [],
                }, pid)

        await presence_service.set_status(user_id, "online")
        await sio.emit("presence:user_status", {"user_id": user_id, "status": "online"})

        # Update / clear authz shadow.
        from app.services.call_signal_authz import call_signal_authz
        if call.status == "ended":
            call_signal_authz.clear(call_id)
            async with async_session_factory() as db:
                await call_service.persist_call_log(db, call)
        else:
            call_signal_authz.remove_participant(call_id, user_id)

        return {"status": "left"}

    except ValueError as e:
        return {"error": str(e)}


# ── WebRTC Signaling Relay ───────────────────────────────

def _authorize_signal(user_id: str, target_id: str) -> str | None:
    """Return the call_id the signal is authorized under, or None.

    Two paths:
      1. Local ActiveCall — the canonical case where this server initiated
         or hosts the call_service entry.
      2. Cross-server shadow — when ActiveCall lives on a different Helen
         server in the federation, the call_signal_authz registry holds
         a minimal participant set that authorizes signal relay through
         this server.
    """
    call = call_service.get_user_call(user_id)
    if call and target_id in call.participants:
        return call.call_id
    # Fall back to the cross-server shadow. We don't know the call_id in
    # the legacy v1 signal payload, so we scan the shadow's local view.
    from app.services.call_signal_authz import call_signal_authz
    # Use the registry's snapshot to find any active call where both
    # users are present. Bounded: typical per-server shadow size << 1k.
    with call_signal_authz._lock:  # internal access is safe — single module
        for cid, sh in list(call_signal_authz._shadows.items()):
            if user_id in sh.participants and target_id in sh.participants:
                return cid
    return None


@sio.event
async def signal_offer(sid: str, data: dict):
    """[DEPRECATED] v1 signaling — use the unified `call_signal` event."""
    user_id = await get_user_id(sid)
    logger.warning("deprecated_v1_handler_called",
                   handler="signal_offer", user_id=user_id)
    return  # silently ignore — clients on v2 don't reach here anyway


@sio.event
async def signal_answer(sid: str, data: dict):
    """[DEPRECATED] v1 signaling — use the unified `call_signal` event."""
    user_id = await get_user_id(sid)
    logger.warning("deprecated_v1_handler_called",
                   handler="signal_answer", user_id=user_id)
    return


@sio.event
async def signal_ice_candidate(sid: str, data: dict):
    """[DEPRECATED] v1 signaling — use the unified `call_signal` event."""
    user_id = await get_user_id(sid)
    logger.warning("deprecated_v1_handler_called",
                   handler="signal_ice_candidate", user_id=user_id)
    return


# ── Mute / Video / Screen Share Toggles ─────────────────

@sio.event
async def call_toggle_mute(sid: str, data: dict):
    """
    Toggle audio mute.
    data: { muted: bool }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    muted = data.get("muted", False)
    call = await call_service.toggle_mute(user_id, muted)

    if call:
        for pid in call.participants:
            if pid != user_id:
                await emit_to_user("call:participant_muted", {
                    "call_id": call.call_id,
                    "user_id": user_id,
                    "muted": muted,
                }, pid)


@sio.event
async def call_toggle_video(sid: str, data: dict):
    """
    Toggle video on/off.
    data: { video_off: bool }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    video_off = data.get("video_off", False)
    call = await call_service.toggle_video(user_id, video_off)

    if call:
        for pid in call.participants:
            if pid != user_id:
                await emit_to_user("call:participant_video", {
                    "call_id": call.call_id,
                    "user_id": user_id,
                    "video_off": video_off,
                }, pid)


@sio.event
async def call_screen_share_start(sid: str, data: dict):
    """
    Notify peers that screen sharing has started.
    data: { call_id?: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call = await call_service.toggle_screen_share(user_id, True)

    if call:
        for pid in call.participants:
            if pid != user_id:
                await emit_to_user("call:screen_share_started", {
                    "call_id": call.call_id,
                    "user_id": user_id,
                }, pid)


@sio.event
async def call_screen_share_stop(sid: str, data: dict):
    """
    Notify peers that screen sharing has stopped.
    data: { call_id?: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call = await call_service.toggle_screen_share(user_id, False)

    if call:
        for pid in call.participants:
            if pid != user_id:
                await emit_to_user("call:screen_share_stopped", {
                    "call_id": call.call_id,
                    "user_id": user_id,
                }, pid)


# ══════════════════════════════════════════════════════════
# ── V2 Signaling — Unified handlers for the new CallEngine
# ══════════════════════════════════════════════════════════
#
# The new frontend CallEngine (v2) uses slightly different event names
# and a unified call_signal event instead of separate signal_offer/answer/ice.
# These handlers run alongside the v1 handlers above for backward compatibility.


@sio.event
async def call_signal(sid: str, data: dict):
    """
    Unified WebRTC signaling relay — replaces signal_offer/answer/ice_candidate.
    Routes SDP offers, answers, and ICE candidates to the target peer.

    data: {
        call_id: str,
        target_id: str,
        signal_type: "offer" | "answer" | "ice-candidate" | "renegotiate",
        sdp?: dict,          # for offer/answer
        candidate?: dict,    # for ice-candidate
        sent_at_ms?: int,    # client-supplied wall-clock; used to drop
                              # stale signals after a topology/route
                              # change.
        ttl_ms?: int,        # max age before we drop. Default applied
                              # below; callers can shorten for ICE
                              # candidates whose validity is short.
    }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call_id = data.get("call_id")
    target_id = data.get("target_id")
    signal_type = data.get("signal_type")

    if not target_id or not signal_type:
        return

    # ── Stale signal drop (audit fix) ──
    # If the client tagged the signal with sent_at_ms, refuse signals
    # older than ttl_ms. ICE candidates after a topology switch are
    # dead-on-arrival because the new RTCPeerConnection has different
    # ufrag/pwd; relaying them just causes ICE failures on the remote.
    # Default TTL: 10s for ICE candidates (short — they should propagate
    # within a second on a healthy LAN), 30s for SDP offers/answers
    # (longer because they're 1-shot per renegotiation).
    sent_at_ms = data.get("sent_at_ms")
    if sent_at_ms is not None:
        try:
            sent_at_ms_int = int(sent_at_ms)
            default_ttl = 10_000 if signal_type == "ice-candidate" else 30_000
            ttl_ms = int(data.get("ttl_ms") or default_ttl)
            now_ms = int(time.time() * 1000)
            age_ms = now_ms - sent_at_ms_int
            if age_ms > ttl_ms:
                logger.info(
                    "call_signal_stale_dropped",
                    user_id=user_id,
                    target_id=target_id,
                    signal_type=signal_type,
                    age_ms=age_ms,
                    ttl_ms=ttl_ms,
                )
                return
        except (ValueError, TypeError):
            # Malformed sent_at_ms — let it through; better to over-deliver
            # a possibly stale signal than to swallow a typed bug.
            pass

    # SECURITY: Validate user is allowed to signal target. Two paths:
    #  (1) Local ActiveCall — canonical, in-memory call_service entry.
    #  (2) Cross-server authz shadow — populated when call lifecycle
    #      events arrive via /api/federation/emit; lets a relay node
    #      forward signals between two users whose call_service state
    #      lives on a *different* Helen server.
    from app.services.call_signal_authz import call_signal_authz
    authorized = False
    if call_id:
        call = call_service.get_call(call_id)
        if call and user_id in call.participants and target_id in call.participants:
            authorized = True
        elif call_signal_authz.is_authorized(call_id, user_id, target_id):
            authorized = True
    else:
        user_call = call_service.get_user_call(user_id)
        if user_call and target_id in user_call.participants:
            authorized = True
            call_id = user_call.call_id
        else:
            # Fall back to scanning the shadow for any call that grants
            # this pair signaling rights.
            resolved = _authorize_signal(user_id, target_id)
            if resolved is not None:
                authorized = True
                call_id = resolved

    if not authorized:
        from app.core.audit import audit_call_signal_unauthorized
        audit_call_signal_unauthorized(user_id, target_id, signal_type)
        logger.warning("call_signal_unauthorized",
                       user_id=user_id, target_id=target_id, call_id=call_id)
        return

    payload = {
        "call_id": call_id,
        "from_id": user_id,
        "signal_type": signal_type,
    }

    if signal_type in ("offer", "answer", "renegotiate"):
        payload["sdp"] = data.get("sdp")
    elif signal_type == "ice-candidate":
        payload["candidate"] = data.get("candidate")

    # Highest-volume event in the system. P0 because realtime media
    # depends on it. Special-cased ACK policy:
    #   * offer / answer / renegotiate → requires_ack=True (one-shot
    #     SDP exchange; lost = call setup fails). Idempotency key
    #     covers retries.
    #   * ice-candidate → requires_ack=False (fire-and-forget; lost
    #     candidate is replaced by the next gathering pulse, ACK is
    #     wasteful capacity at scale). The envelope schema's escape
    #     hatch (allow_p0_no_ack) accepts this.
    from app.services import fabric_emit as _fe
    is_ice = signal_type == "ice-candidate"
    await _fe.emit_event(
        event_type="call_signal",
        priority="P0",
        payload=payload,
        destination_user_id=target_id,
        source_user_id=user_id,
        call_id=call_id,
        idempotency_key=(
            f"call_signal:{call_id}:{user_id}:{target_id}:{signal_type}"
            if not is_ice
            else f"call_ice:{call_id}:{user_id}:{target_id}:{data.get('sent_at_ms', 0)}"
        ),
        requires_ack=not is_ice,
        # ICE candidates: short TTL because stale ones must drop
        # (see call_signal sent_at_ms guard above).
        ttl_ms=2000 if is_ice else 5000,
    )

    logger.debug(
        "call_signal_relayed",
        from_id=user_id,
        target_id=target_id,
        signal_type=signal_type,
    )


# ── V2 Wrappers — aligned event names ────────────────────
# These emit events matching the new frontend's expected names
# (call_incoming, call_accepted, call_rejected, call_hangup, etc.)

@sio.event
async def v2_call_initiate(sid: str, data: dict):
    """
    V2 — Initiate a 1-to-1 call.
    data: { target_id: str, media_type: "audio" | "video" }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    target_id = data.get("target_id")
    media_type = data.get("media_type", "audio")

    if not target_id:
        return {"error": "target_id is required"}

    # SECURITY: Validate target user exists and is active.
    # Cross-server federation: a target hosted on a sibling Helen server
    # won't be in our local DB. Fall back to the federated presence
    # directory (populated by federated_presence_resync) before refusing.
    from app.core.security_utils import is_valid_uuid
    if not is_valid_uuid(target_id):
        return {"error": "Invalid target_id format"}

    from sqlalchemy import select as sa_select
    from app.models.user import User
    target_is_remote = False
    try:
        async with async_session_factory() as db:
            result = await db.execute(sa_select(User).where(User.id == target_id, User.is_active == True))
            target_user = result.scalar_one_or_none()
            if not target_user:
                # Not local — check federated presence directory.
                from app.core.config import get_settings as _get_settings
                if _get_settings().FEDERATION_ENABLED:
                    try:
                        from app.services.federated_presence import federated_presence
                        remote = await federated_presence.get(target_id)
                        if remote is not None:
                            target_is_remote = True
                        else:
                            return {"error": "Target user not found or inactive"}
                    except Exception as _fe:
                        logger.warning("federated_presence_lookup_failed",
                                       target_id=target_id, error=str(_fe))
                        return {"error": "Target user not found or inactive"}
                else:
                    return {"error": "Target user not found or inactive"}

            # ── Block enforcement (LOCAL only — remote blocks aren't replicated) ──
            if not target_is_remote:
                from app.services.user_service import UserService as _US
                blocked, blocker = await _US.is_blocked_either_way(db, user_id, target_id)
                if blocked:
                    from app.core.audit import audit_call_signal_unauthorized
                    audit_call_signal_unauthorized(user_id, target_id, "v2_call_initiate_blocked")
                    if blocker == user_id:
                        return {"error": "You have blocked this user. Unblock them to call."}
                    return {"error": "You cannot call this user."}
    except Exception as e:
        logger.error("v2_call_initiate_target_check_error", error=str(e))
        return {"error": "Failed to validate target user"}

    try:
        call = await call_service.initiate_call(
            initiator_id=user_id,
            call_type=media_type,
            routing="p2p",
        )

        # Resolve caller identity from the DB so the callee's UI can show
        # a real username + share_code. The previous code shipped the
        # raw `user_id` UUID as `caller_name`, leaving the callee with
        # nothing human-readable.
        caller_username    = None
        caller_display     = None
        caller_share_code  = None
        try:
            async with async_session_factory() as db:
                row = (
                    await db.execute(
                        sa_select(User).where(User.id == user_id)
                    )
                ).scalar_one_or_none()
                if row is not None:
                    caller_username   = row.username
                    caller_display    = row.display_name or row.username
                    caller_share_code = row.share_code
        except Exception as e:
            logger.warning("call_caller_lookup_failed", error=str(e))

        caller_name = caller_display or caller_username or user_id

        # Seed the cross-server signal-authz shadow up-front so signaling
        # works in BOTH directions even when callee lives on a sibling
        # Helen server (where call_service has no record of this call).
        _local_authz_seed(call.call_id, [user_id, target_id])

        # Arm the no-answer timer (30s default) — caller's UI is freed
        # automatically if the callee never accepts/rejects.
        _schedule_missed_call_timeout(call.call_id, user_id)

        # Notify callee with v2 event names. Include both the short
        # `caller_username` and the full 64-char `caller_share_code` so
        # the receiving UI can render whichever it prefers (and also
        # show both when the operator wants to verify identity).
        # Canary: when HELEN_FABRIC_EVENT_ALLOWLIST contains
        # "call_incoming" or matches a wildcard, this emit travels via
        # event_envelope + route_executor (tracing, idempotency, ACK,
        # retry, DLQ). Otherwise it falls through to plain
        # emit_to_user — zero behavior change for legacy deployments.
        from app.services import fabric_emit as _fe
        await _fe.emit_event(
            event_type="call_incoming",
            priority="P1",
            payload={
                "call_id":            call.call_id,
                "caller_id":          user_id,
                "caller_name":        caller_name,
                "caller_username":    caller_username,
                "caller_share_code":  caller_share_code,
                "media_type":         media_type,
            },
            destination_user_id=target_id,
            source_user_id=user_id,
            call_id=call.call_id,
            idempotency_key=f"call_initiate:{call.call_id}",
            requires_ack=True,
        )

        await presence_service.set_status(user_id, "in_call")
        await sio.emit("presence:user_status", {
            "user_id": user_id,
            "status": "in_call",
        })

        return {"call_id": call.call_id}

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def v2_call_accept(sid: str, data: dict):
    """
    V2 — Accept an incoming call.
    data: { call_id: str, caller_id: str, idempotency_key?: str }

    Idempotent: a duplicate accept (double-tap, retry after timeout)
    returns the same response as the first call rather than creating
    duplicate participant rows or racing the call-state activation.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    # Idempotency: prefer the client-supplied key, fall back to a
    # deterministic one derived from (user, call). Latter still
    # protects double-clicks but the explicit key is required for
    # different requests-with-same-business-effect to coexist.
    #
    # CRITICAL: the cache wraps BOTH paths (local accept + cross-server
    # forward). Without this, a double-tap accept on a remote callee
    # would fire two RPCs to the origin server. The first wins on the
    # origin (also idempotency-cached there), but we'd still pay two
    # signed HTTP round-trips and confuse latency telemetry.
    from app.services.idempotency_cache import idempotency
    idempo_key = data.get("idempotency_key") or f"{user_id}:accept"

    async def _do_accept():
        # Cross-server bridge: when ActiveCall lives on a sibling Helen
        # server (callee here, initiator there), forward the accept to
        # the owning server and return its response.
        forwarded, fwd_resp = await _maybe_forward_to_origin(
            call_id, "accept", user_id,
            extra={"idempotency_key": idempo_key},
        )
        if forwarded:
            if fwd_resp and fwd_resp.get("status") == "accepted":
                await presence_service.set_status(user_id, "in_call")
                await sio.emit("presence:user_status", {
                    "user_id": user_id, "status": "in_call",
                })
            return fwd_resp or {"error": "forward_failed"}
        return await _v2_call_accept_impl(sid, data, user_id, call_id)

    try:
        return await idempotency.get_or_compute(call_id, idempo_key, _do_accept)
    except Exception as exc:
        logger.error("v2_call_accept_failed", call_id=call_id, user_id=user_id, error=str(exc))
        return {"error": "accept_failed", "detail": str(exc)}


async def _v2_call_accept_impl(sid: str, data: dict, user_id: str, call_id: str):
    """Real accept logic — wrapped by v2_call_accept for idempotency."""
    try:
        call = await call_service.accept_call(call_id, user_id)

        try:
            init_ice = build_ice_config(call.initiator_id)
        except Exception:
            init_ice = {"ice_servers": [], "ice_transport_policy": "all"}
        try:
            callee_ice = build_ice_config(user_id)
        except Exception:
            callee_ice = {"ice_servers": [], "ice_transport_policy": "all"}

        # Refresh authz shadow with the confirmed participant set
        # (covers any peer that joined while the accept was in flight).
        _local_authz_seed(call_id, list(call.participants.keys()))

        # Notify initiator with v2 event (fabric-aware via canary).
        from app.services import fabric_emit as _fe
        await _fe.emit_event(
            event_type="call_accepted",
            priority="P1",
            payload={
                "call_id": call_id,
                "callee_id": user_id,
                "ice_servers": init_ice["ice_servers"],
                "ice_transport_policy": init_ice["ice_transport_policy"],
                "ice_ttl_seconds": init_ice.get("ttl_seconds"),
            },
            destination_user_id=call.initiator_id,
            source_user_id=user_id,
            call_id=call_id,
            idempotency_key=f"call_accepted:{call_id}:{user_id}",
            requires_ack=True,
        )

        await presence_service.set_status(user_id, "in_call")
        await sio.emit("presence:user_status", {
            "user_id": user_id,
            "status": "in_call",
        })

        return {
            "status": "accepted",
            "ice_servers": callee_ice["ice_servers"],
            "ice_transport_policy": callee_ice["ice_transport_policy"],
            "ice_ttl_seconds": callee_ice.get("ttl_seconds"),
        }

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def v2_call_reinvite(sid: str, data: dict):
    """
    Re-invite a participant who declined or missed the original call.

    data: { call_id: str, target_user_id: str }

    Authorization: only the call's host (= initiator currently) may
    re-invite. The original invite row is updated back to 'ringing'
    and a fresh `call_incoming` is pushed to the target's sockets.
    Idempotent: re-issuing while already ringing is a no-op.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    target_id = data.get("target_user_id")
    if not call_id or not target_id:
        return {"error": "call_id and target_user_id required"}

    forwarded, fwd_resp = await _maybe_forward_to_origin(
        call_id, "reinvite", user_id,
        extra={"target_user_id": target_id},
    )
    if forwarded:
        return fwd_resp or {"error": "forward_failed"}

    try:
        call = call_service.get_call(call_id)
        if not call:
            return {"error": "call_not_found"}

        # Auth: must be host (the initiator). Future: also allow
        # role='moderator' if we ever expose that.
        if call.initiator_id != user_id:
            return {"error": "forbidden — only the host can re-invite"}

        # Push a fresh `call_incoming` event to the target's sockets.
        # We reuse the same payload format the initial ring uses.
        async with async_session_factory() as db:
            from sqlalchemy import select as _sel
            from app.models.user import User as _User
            caller_row = (await db.execute(
                _sel(_User).where(_User.id == user_id)
            )).scalar_one_or_none()

        caller_display    = caller_row.display_name or caller_row.username if caller_row else user_id
        caller_username   = caller_row.username if caller_row else None
        caller_share_code = caller_row.share_code if caller_row else None

        incoming_payload = {
            "call_id":           call.call_id,
            "caller_id":         user_id,
            "media_type":        call.media_type,
            "channel_id":        call.channel_id,
            "caller_name":       caller_display,
            "caller_username":   caller_username,
            "caller_share_code": caller_share_code,
            "is_reinvite":       True,
        }

        # emit_to_user handles local fan-out plus cross-server federation
        # fallback. Returns the count actually delivered (0 = offline /
        # unreachable on every known peer).
        delivered = await emit_to_user("call_incoming", incoming_payload, target_id)
        if delivered == 0:
            return {"error": "target_offline", "detail": "user has no active sockets"}

        # Make sure the authz shadow knows about the (possibly remote)
        # target so its return signals can be relayed.
        from app.services.call_signal_authz import call_signal_authz
        call_signal_authz.add_participant(call_id, target_id)

        logger.info("participant_reinvited", call_id=call_id, target=target_id, by=user_id)
        return {"status": "ok", "delivered_to_sockets": delivered}

    except Exception as exc:
        logger.error("v2_call_reinvite_failed", call_id=call_id, target=target_id, error=str(exc))
        return {"error": "reinvite_failed", "detail": str(exc)}


@sio.event
async def v2_call_reconnect(sid: str, data: dict):
    """
    Restore a participant's session after a transient WebSocket drop.

    data: { call_id: str, last_seq: int }

    The client supplies the last `seq` it processed before disconnection;
    server returns:
      • current participant list
      • events since `last_seq` so the UI can fast-forward state without
        a hard refresh

    Idempotent: re-running with the same last_seq is safe.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    last_seq = int(data.get("last_seq") or 0)
    if not call_id:
        return {"error": "call_id required"}

    call = call_service.get_call(call_id)
    if not call:
        return {"error": "call_ended"}

    # Authorization — must be a current participant (or the host)
    if user_id not in call.participants and user_id != call.initiator_id:
        return {"error": "not_a_participant"}

    # Re-attach this socket to the call's room so future emits reach us.
    call_room = f"call:{call_id}"
    try:
        await sio.enter_room(sid, call_room)
    except Exception:
        pass

    missed = call.events_since(last_seq, limit=500)
    logger.info(
        "v2_call_reconnect",
        call_id=call_id, user_id=user_id,
        last_seq=last_seq, current_seq=call.current_sequence,
        replayed=len(missed),
    )
    return {
        "status": "rejoined",
        "current_seq": call.current_sequence,
        "current_members": list(call.participants.keys()),
        "missed_events": missed,
        "host_id": call.initiator_id,
    }


@sio.event
async def v2_call_reject(sid: str, data: dict):
    """
    V2 — Reject an incoming call.
    data: { call_id: str, caller_id: str, idempotency_key?: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    # Idempotency wrapping (audit fix). Without it, a client retrying a
    # reject after an ack-loss creates duplicate rejected_call events
    # and double-clears state. Reuse the same cache the accept path
    # already uses; key shape is (call_id, idempotency_key).
    idempotency_key = data.get("idempotency_key")
    if idempotency_key:
        from app.services.idempotency_cache import idempotency
        return await idempotency.get_or_compute(
            call_id,
            f"reject:{idempotency_key}",
            factory=lambda: _v2_call_reject_inner(call_id, user_id),
        )
    return await _v2_call_reject_inner(call_id, user_id)


async def _v2_call_reject_inner(call_id: str, user_id: str):
    forwarded, fwd_resp = await _maybe_forward_to_origin(call_id, "reject", user_id)
    if forwarded:
        return fwd_resp or {"error": "forward_failed"}

    try:
        call = await call_service.reject_call(call_id, user_id)

        from app.services import fabric_emit as _fe
        await _fe.emit_event(
            event_type="call_rejected",
            priority="P1",
            payload={
                "call_id": call_id,
                "user_id": user_id,
            },
            destination_user_id=call.initiator_id,
            source_user_id=user_id,
            call_id=call_id,
            idempotency_key=f"call_rejected:{call_id}:{user_id}",
            requires_ack=True,
        )

        await presence_service.set_status(call.initiator_id, "online")
        await sio.emit("presence:user_status", {
            "user_id": call.initiator_id,
            "status": "online",
        })

        # Tear down the cross-server authz shadow so stale signaling
        # is rejected. Persist the call log alongside.
        from app.services.call_signal_authz import call_signal_authz
        call_signal_authz.clear(call_id)
        async with async_session_factory() as db:
            await call_service.persist_call_log(db, call)

        return {"status": "rejected"}

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def v2_call_hangup(sid: str, data: dict):
    """
    V2 — Hang up current call.
    data: { call_id: str, target_id?: str, idempotency_key?: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    idempotency_key = data.get("idempotency_key")
    if idempotency_key:
        from app.services.idempotency_cache import idempotency
        return await idempotency.get_or_compute(
            call_id,
            f"hangup:{idempotency_key}",
            factory=lambda: _v2_call_hangup_inner(call_id, user_id),
        )
    return await _v2_call_hangup_inner(call_id, user_id)


async def _v2_call_hangup_inner(call_id: str, user_id: str):
    forwarded, fwd_resp = await _maybe_forward_to_origin(call_id, "hangup", user_id)
    if forwarded:
        if fwd_resp and fwd_resp.get("status") == "ended":
            await presence_service.set_status(user_id, "online")
            await sio.emit("presence:user_status", {
                "user_id": user_id, "status": "online",
            })
        return fwd_resp or {"error": "forward_failed"}

    try:
        call = call_service.get_call(call_id)
        if not call:
            return {"error": "Call not found"}

        from app.services import fabric_emit as _fe
        for pid in list(call.participants.keys()):
            if pid != user_id:
                await _fe.emit_event(
                    event_type="call_hangup",
                    priority="P1",
                    payload={
                        "call_id": call_id,
                        "ended_by": user_id,
                        "reason": "hangup",
                    },
                    destination_user_id=pid,
                    source_user_id=user_id,
                    call_id=call_id,
                    idempotency_key=f"call_hangup:{call_id}:{user_id}",
                    requires_ack=True,
                )

            await presence_service.set_status(pid, "online")
            await sio.emit("presence:user_status", {
                "user_id": pid,
                "status": "online",
            })

        ended_call = await call_service.hangup(call_id, user_id)

        # Drop the cross-server signal-authz shadow — the call is over,
        # any further signal relays should be rejected.
        from app.services.call_signal_authz import call_signal_authz
        call_signal_authz.clear(call_id)

        async with async_session_factory() as db:
            await call_service.persist_call_log(db, ended_call)

        # ── Active-call channel broadcast (Join Existing Call UX) ─────
        # When a group call ends via hangup (host ends for everyone),
        # tell every channel member so the UI removes the "Join Call"
        # affordance immediately. Mirrors the same broadcast emitted
        # from v2_call_leave_group when the call empties out.
        if ended_call.channel_id:
            _ended_payload_hu = {
                "channel_id": ended_call.channel_id,
                "call_id": call_id,
                "ended_by": user_id,
                "reason": "hangup",
            }
            try:
                from app.socket.channel_room import (
                    room_name as _chan_room_name_hu,
                )
                await sio.emit(
                    "channel:active_call_ended",
                    _ended_payload_hu,
                    room=_chan_room_name_hu(ended_call.channel_id),
                )
            except Exception as _e_hu:
                logger.warning(
                    "active_call_end_broadcast_hangup_failed",
                    channel_id=ended_call.channel_id, error=str(_e_hu),
                )

            async def _broadcast_active_call_ended_hangup_remote() -> None:
                try:
                    from sqlalchemy import select as _sel_hu
                    from app.models.channel import ChannelMember as _CM_hu
                    async with async_session_factory() as _db_hu:
                        rows = (await _db_hu.execute(
                            _sel_hu(_CM_hu.user_id).where(
                                _CM_hu.channel_id == ended_call.channel_id
                            )
                        )).all()
                    member_ids = [r[0] for r in rows]
                    import asyncio as _asyncio_hu
                    await _asyncio_hu.gather(
                        *(emit_to_user(
                            "channel:active_call_ended",
                            _ended_payload_hu,
                            mid,
                        ) for mid in member_ids
                          if not presence_service.get_sids(mid)),
                        return_exceptions=True,
                    )
                except Exception as _e_hu2:
                    logger.debug(
                        "active_call_end_broadcast_hangup_remote_failed",
                        channel_id=ended_call.channel_id, error=str(_e_hu2),
                    )

            import asyncio as _asyncio_hu_outer
            _hu_bcast_task = _asyncio_hu_outer.create_task(
                _broadcast_active_call_ended_hangup_remote()
            )
            _hu_bcast_task.set_name(f"active_call_ended_hangup:{call_id}")
            try:
                from app.services.call_service import call_service as _cs_hu
                _cs_hu._bg_tasks.add(_hu_bcast_task)
                _hu_bcast_task.add_done_callback(_cs_hu._bg_tasks.discard)
            except Exception:
                pass

        return {"status": "ended"}

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def v2_call_join_group(sid: str, data: dict):
    """
    V2 — Join or create a group call.
    data: { channel_id: str, media_type: "audio" | "video" }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    channel_id = data.get("channel_id")
    media_type = data.get("media_type", "audio")

    if not channel_id:
        return {"error": "channel_id is required"}

    # SECURITY: Verify channel membership before allowing group call join
    try:
        async with async_session_factory() as db:
            if not await ChannelService.is_member(db, channel_id, user_id):
                from app.core.audit import audit_permission_denied
                audit_permission_denied(user_id, f"channel:{channel_id}", "v2_call_join_group")
                logger.warning("v2_call_join_group_unauthorized", user_id=user_id, channel_id=channel_id)
                return {"error": "Not a member of this channel"}
    except Exception as e:
        logger.error("v2_call_join_group_membership_check_error", error=str(e))
        return {"error": "Failed to verify channel membership"}

    # Block-aware join: don't let A join a group call that already has B
    # in it if either of them blocked the other. Privacy-respecting
    # message — doesn't disclose whether you blocked or were blocked.
    try:
        existing_pre = call_service.get_call_by_channel(channel_id)
        if existing_pre is not None:
            from app.services.user_service import UserService as _US
            async with async_session_factory() as db:
                for pid in list(existing_pre.participants.keys()):
                    if pid == user_id:
                        continue
                    blocked, _ = await _US.is_blocked_either_way(db, user_id, pid)
                    if blocked:
                        from app.core.audit import audit_call_signal_unauthorized
                        audit_call_signal_unauthorized(
                            user_id, pid, "v2_call_join_group_blocked",
                        )
                        return {
                            "error": "Cannot join: a participant is unavailable to you.",
                        }
    except Exception as e:
        logger.warning("group_call_block_check_failed", error=str(e))

    # Cross-server discovery (BLOCKER-2 fix). Before creating a new
    # local ActiveCall, ask the DB whether ANY server in the
    # federation already hosts an active call for this channel. If
    # one exists on a sibling server, forward the join via
    # /api/federation/call/rpc instead of forking a parallel call.
    try:
        from app.services.call_state_persistence import call_state_persistence as _csp_join
        from app.services.discovery_service import get_server_id as _my_id_join
        existing_db = await _csp_join.get_active_by_channel(channel_id)
        my_id = _my_id_join()
        if existing_db is not None:
            origin = existing_db.get("origin_server_id")
            existing_call_id = existing_db["call_id"]
            if origin and origin != my_id and call_service.get_call(existing_call_id) is None:
                # Remote-origin call — forward join via federation.
                logger.info(
                    "v2_call_join_group_forwarding",
                    call_id=existing_call_id, origin=origin, user_id=user_id,
                )
                forwarded, fwd_resp = await _maybe_forward_to_origin(
                    existing_call_id, "join", user_id,
                    extra={"channel_id": channel_id, "media_type": media_type},
                )
                if forwarded:
                    if fwd_resp and fwd_resp.get("status") in ("joined", "already_in_call"):
                        await presence_service.set_status(user_id, "in_call")
                    return fwd_resp or {"error": "forward_failed"}
    except Exception as _join_e:
        logger.warning("v2_call_join_group_cross_server_lookup_failed",
                       channel_id=channel_id, error=str(_join_e))

    try:
        existing = call_service.get_call_by_channel(channel_id)
        is_new_call = existing is None

        if existing:
            call = await call_service.join_group_call(existing.call_id, user_id)
        else:
            call = await call_service.initiate_call(
                initiator_id=user_id,
                call_type=media_type,
                routing="mesh",
                channel_id=channel_id,
            )

        # Join this sid (and any other sids of the same user) into the call
        # room. One emit(room=...) then reaches all participants via socket.io's
        # native fan-out instead of O(N) per-sid emits.
        call_room = f"call:{call.call_id}"
        for u_sid in presence_service.get_sids(user_id):
            try:
                await sio.enter_room(u_sid, call_room)
            except Exception:
                pass

        # Build joiner's ICE config once (only they need it in the ack).
        try:
            joiner_ice = build_ice_config(user_id)
        except Exception:
            joiner_ice = {"ice_servers": [], "ice_transport_policy": "all"}

        # Broadcast to every other participant that the new user joined —
        # but only for calls small enough that a steady per-join stream is
        # useful UX. At scale (>200), the stream collapses into noise
        # AND the fanout is O(N²) across a burst of joins, which queues
        # millions of pending emits and starves the event loop long enough
        # for clients to hit engineio ping timeout and silently drop.
        # Large calls should rely on a polled participants-digest instead.
        import asyncio as _asyncio_join
        _LARGE_CALL_THRESHOLD = 200
        if len(call.participants) <= _LARGE_CALL_THRESHOLD:
            _asyncio_join.create_task(sio.emit(
                "call_participant_joined",
                {
                    "call_id": call.call_id,
                    "user_id": user_id,
                    "channel_id": channel_id,
                },
                room=call_room,
                skip_sid=sid,
            ))

            # Cross-server fan-out: peers hosted on a sibling Helen
            # server aren't in this server's call_room, so the room
            # broadcast misses them. fabric_emit (envelope + tracing
            # + ACK + DLQ when in allowlist; legacy emit_to_user
            # otherwise) falls back to federation only for users with
            # no local sids — co-located peers already got it via the
            # room emit above.
            async def _cross_server_join_fanout():
                try:
                    from app.services import fabric_emit as _fe
                    payload = {
                        "call_id":    call.call_id,
                        "user_id":    user_id,
                        "channel_id": channel_id,
                    }
                    for pid in list(call.participants.keys()):
                        if pid == user_id:
                            continue
                        if presence_service.get_sids(pid):
                            continue  # local — already delivered
                        try:
                            await _fe.emit_event(
                                event_type="call_participant_joined",
                                priority="P1",
                                payload=payload,
                                destination_user_id=pid,
                                source_user_id=user_id,
                                call_id=call.call_id,
                                channel_id=channel_id,
                                idempotency_key=f"call_join:{call.call_id}:{user_id}:{pid}",
                                requires_ack=True,
                            )
                        except Exception as _e:
                            logger.debug(
                                "cross_server_join_fanout_failed",
                                user_id=pid, error=str(_e),
                            )
                except Exception as _e:
                    logger.debug(
                        "cross_server_join_fanout_outer_failed",
                        error=str(_e),
                    )

            _asyncio_join.create_task(_cross_server_join_fanout())

        # Ring channel members who aren't in the call — only on call creation
        # AND only for channels small enough that a per-member ring makes sense.
        # For megachannels, ringing 10k members amounts to a broadcast storm
        # that would starve the event loop mid-join-burst and cause peer
        # sockets to hit ping timeout silently. Large channels should surface
        # the incoming call via a lighter-weight digest (e.g. the channel's
        # unread-indicator bump that the chat message itself triggers).
        _RING_CHANNEL_THRESHOLD = 500
        channel_member_count = None
        if is_new_call:
            try:
                from sqlalchemy import func as _sql_func, select as _sel_count
                from app.models.channel import ChannelMember as _CM_count
                async with async_session_factory() as _count_db:
                    channel_member_count = (await _count_db.execute(
                        _sel_count(_sql_func.count()).select_from(_CM_count).where(
                            _CM_count.channel_id == channel_id
                        )
                    )).scalar_one()
            except Exception:
                channel_member_count = None
        # Ring members who aren't in the call yet. We used to gate this
        # behind `is_new_call`, but that meant: if a zombie call from a
        # previous test/session was still alive, no one ever got the
        # ring on subsequent join attempts. Today the inner filter
        # (`r[0] not in call.participants`) already prevents re-ringing
        # users who are already in the call, so the outer `is_new_call`
        # gate was strictly harmful.
        if channel_member_count is None or channel_member_count <= _RING_CHANNEL_THRESHOLD:
            async def _ring_members():
                try:
                    from sqlalchemy import select as _sel
                    from app.models.channel import ChannelMember as _CM
                    from app.models.user import User as _User
                    async with async_session_factory() as db:
                        rows = (await db.execute(
                            _sel(_CM.user_id).where(_CM.channel_id == channel_id)
                        )).all()
                        # Resolve the caller's identity here too, so the
                        # group-call ring path stops shipping the raw UUID
                        # as `caller_name` (matching the DM-call fix).
                        # Use `_sel`/`_User` from this scope; `sa_select` and
                        # `User` are defined inside `v2_call_initiate`, not
                        # this nested coroutine.
                        caller_row = (await db.execute(
                            _sel(_User).where(_User.id == user_id)
                        )).scalar_one_or_none()
                    member_ids = [r[0] for r in rows if r[0] not in call.participants]
                    if caller_row is not None:
                        _caller_username   = caller_row.username
                        _caller_display    = caller_row.display_name or caller_row.username
                        _caller_share_code = caller_row.share_code
                    else:
                        _caller_username = _caller_display = _caller_share_code = None
                    incoming_payload = {
                        "call_id":            call.call_id,
                        "caller_id":          user_id,
                        "media_type":         media_type,
                        "channel_id":         channel_id,
                        "caller_name":        _caller_display or _caller_username or user_id,
                        "caller_username":    _caller_username,
                        "caller_share_code":  _caller_share_code,
                    }
                    # Seed the authz shadow with every channel member that
                    # could potentially accept — once a remote member's
                    # client returns an accept, their server has the
                    # required signaling rights to relay offers/answers.
                    _local_authz_seed(call.call_id, [user_id, *member_ids])

                    import asyncio as _asyncio_ring
                    if member_ids:
                        # emit_to_user falls back to federation when the
                        # member is hosted on a sibling Helen server, so
                        # cross-server channel rings now work too.
                        await _asyncio_ring.gather(
                            *(emit_to_user("call_incoming", incoming_payload, mid)
                              for mid in member_ids),
                            return_exceptions=True,
                        )
                except Exception as exc:
                    logger.warning("v2_call_join_ring_failed", error=str(exc))

            # Track the spawned task in `call_service._bg_tasks` so the
            # server's shutdown drain catches it and exceptions don't
            # evaporate into asyncio's root logger. Without this, a DB
            # blip during the ring fan-out left the call un-rung and
            # the operator had no signal in helen-server logs.
            import asyncio as _asyncio_ring_outer
            _ring_task = _asyncio_ring_outer.create_task(_ring_members())
            _ring_task.set_name(f"ring_members:{call.call_id}")
            try:
                from app.services.call_service import call_service as _cs
                _cs._bg_tasks.add(_ring_task)
                _ring_task.add_done_callback(_cs._bg_tasks.discard)
            except Exception:
                # If call_service isn't importable here for any reason
                # (circular import edge case), fall back to a logged
                # done-callback so failures aren't fully silent.
                def _log_failure(t: "_asyncio_ring_outer.Task") -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        logger.error("ring_members_unhandled_exc", error=str(exc))
                _ring_task.add_done_callback(_log_failure)

        # ── Active-call channel broadcast (Join Existing Call UX) ─────────
        # Notify every channel member — even those not currently in the
        # call — that a group call is now LIVE in this channel. The UI
        # uses this signal to render the "Join Call" affordance in the
        # channel header / QuickCallSheet without polling. The matching
        # channel:active_call_ended is emitted from v2_call_leave_group
        # when the call transitions to ended.
        if is_new_call:
            _channel_active_call_payload = {
                "channel_id": channel_id,
                "call_id": call.call_id,
                "call_type": media_type,
                "routing": call.routing,
                "started_by": user_id,
                "started_at": call.started_at.isoformat() if call.started_at else None,
                "participant_count": len(call.participants),
            }
            try:
                # Local channel room — covers all online members on this server.
                from app.socket.channel_room import (
                    ensure_populated as _ensure_chan_room,
                    room_name as _chan_room_name,
                )
                await _ensure_chan_room(sio, channel_id)
                await sio.emit(
                    "channel:active_call_started",
                    _channel_active_call_payload,
                    room=_chan_room_name(channel_id),
                )
            except Exception as _e:
                logger.warning(
                    "active_call_broadcast_local_failed",
                    channel_id=channel_id, error=str(_e),
                )

            # Cross-server fan-out — federation peers don't share rooms,
            # so we explicitly emit_to_user for off-server channel
            # members. Same threshold-gate as the ring path: if the
            # channel has more than _RING_CHANNEL_THRESHOLD members,
            # skip per-member federation (millions of emits would
            # starve the event loop). Local room broadcast above still
            # covers locally-connected members at O(1).
            async def _broadcast_active_call_started_remote() -> None:
                try:
                    if (channel_member_count is not None and
                            channel_member_count > _RING_CHANNEL_THRESHOLD):
                        logger.info(
                            "active_call_remote_broadcast_skipped_threshold",
                            channel_id=channel_id,
                            member_count=channel_member_count,
                            threshold=_RING_CHANNEL_THRESHOLD,
                        )
                        return
                    from sqlalchemy import select as _sel_acs
                    from app.models.channel import ChannelMember as _CM_acs
                    async with async_session_factory() as _db_acs:
                        rows = (await _db_acs.execute(
                            _sel_acs(_CM_acs.user_id).where(
                                _CM_acs.channel_id == channel_id
                            )
                        )).all()
                    member_ids = [r[0] for r in rows]
                    import asyncio as _asyncio_acs
                    await _asyncio_acs.gather(
                        *(emit_to_user(
                            "channel:active_call_started",
                            _channel_active_call_payload,
                            mid,
                        ) for mid in member_ids
                          if not presence_service.get_sids(mid)),
                        return_exceptions=True,
                    )
                except Exception as _e:
                    logger.debug(
                        "active_call_broadcast_remote_failed",
                        channel_id=channel_id, error=str(_e),
                    )

            import asyncio as _asyncio_acs_outer
            _bcast_task = _asyncio_acs_outer.create_task(
                _broadcast_active_call_started_remote()
            )
            _bcast_task.set_name(f"active_call_started:{call.call_id}")
            try:
                from app.services.call_service import call_service as _cs_bcast
                _cs_bcast._bg_tasks.add(_bcast_task)
                _bcast_task.add_done_callback(_cs_bcast._bg_tasks.discard)
            except Exception:
                pass

        await presence_service.set_status(user_id, "in_call")
        # Same large-call gate as above — skip per-join status churn at scale.
        if len(call.participants) <= _LARGE_CALL_THRESHOLD:
            _asyncio_join.create_task(sio.emit(
                "presence:user_status",
                {"user_id": user_id, "status": "in_call"},
                room=call_room,
            ))

        # ── Topology orchestration ──────────────────────────────
        # Tell the SFU orchestrator + large-call orchestrator about
        # the new participant count so they can decide whether to
        # upgrade the topology (mesh→sfu_small→sfu_large→…→federated_
        # webinar). Both orchestrators are best-effort and broadcast
        # their own ``call:topology_change`` event when a switch fires.
        try:
            from app.services.sfu_orchestrator import get_orchestrator as _sfu_get
            _asyncio_join.create_task(
                _sfu_get().observe_participant_count(
                    call.call_id, len(call.participants),
                ),
            )
        except Exception as _orch_e:
            logger.debug("sfu_orch_observe_failed",
                         call_id=call.call_id, error=str(_orch_e))
        try:
            from app.services.large_call_orchestrator import (
                get_large_call_orchestrator as _lco_get,
            )
            _asyncio_join.create_task(
                _lco_get().on_join(call.call_id, user_id),
            )
        except Exception as _orch_e:
            logger.debug("large_call_orch_observe_failed",
                         call_id=call.call_id, error=str(_orch_e))

        participant_list = [{"user_id": pid} for pid in call.participants.keys()]
        return {
            "call_id": call.call_id,
            "participants": participant_list,
            "ice_servers": joiner_ice["ice_servers"],
            "ice_transport_policy": joiner_ice["ice_transport_policy"],
            "ice_ttl_seconds": joiner_ice.get("ttl_seconds"),
        }

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def v2_call_leave_group(sid: str, data: dict):
    """
    V2 — Leave a group call.
    data: { call_id: str, channel_id?: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    forwarded, fwd_resp = await _maybe_forward_to_origin(
        call_id, "leave", user_id,
        extra={"channel_id": data.get("channel_id")},
    )
    if forwarded:
        if fwd_resp and fwd_resp.get("status") == "left":
            await presence_service.set_status(user_id, "online")
            await sio.emit("presence:user_status", {
                "user_id": user_id, "status": "online",
            })
        return fwd_resp or {"error": "forward_failed"}

    try:
        pre_call = call_service.get_call(call_id)
        if not pre_call:
            return {"error": "Call not found"}

        # Snapshot the participant set BEFORE leave_call removes us so
        # we can fan out to remote-server peers (room broadcast can't
        # reach them). The local room emit handles co-located peers.
        pre_participants = list(pre_call.participants.keys())

        call = await call_service.leave_call(call_id, user_id)
        call_room = f"call:{call_id}"

        # Tell the orchestrators someone left so the topology can
        # downgrade (e.g. sfu_small → mesh) when the call shrinks.
        try:
            import asyncio as _asyncio_orch_leave
            from app.services.sfu_orchestrator import get_orchestrator as _sfu_get_l
            from app.services.large_call_orchestrator import (
                get_large_call_orchestrator as _lco_get_l,
            )
            remaining = len(call.participants) if call else 0
            _asyncio_orch_leave.create_task(
                _sfu_get_l().observe_participant_count(call_id, remaining),
            )
            _asyncio_orch_leave.create_task(
                _lco_get_l().on_leave(call_id, user_id),
            )
        except Exception as _orch_e:
            logger.debug("orch_leave_failed",
                         call_id=call_id, error=str(_orch_e))

        # Single fan-out to the local room (O(1) for co-located peers).
        await sio.emit(
            "call_participant_left",
            {"call_id": call_id, "user_id": user_id},
            room=call_room,
            skip_sid=sid,
        )

        # Cross-server peers aren't in this server's room — explicitly
        # deliver the leave event so their UI updates the participant
        # list. fabric_emit (envelope + tracing + ACK + DLQ when in
        # allowlist; legacy emit_to_user otherwise) handles cross-
        # server delivery.
        from app.services import fabric_emit as _fe
        for pid in pre_participants:
            if pid == user_id:
                continue
            if presence_service.get_sids(pid):
                continue  # local — already got it via room emit
            try:
                await _fe.emit_event(
                    event_type="call_participant_left",
                    priority="P1",
                    payload={"call_id": call_id, "user_id": user_id},
                    destination_user_id=pid,
                    source_user_id=user_id,
                    call_id=call_id,
                    idempotency_key=f"call_left:{call_id}:{user_id}:{pid}",
                    requires_ack=True,
                )
            except Exception as _e:
                logger.debug("cross_server_left_emit_failed", error=str(_e))

        # Leave the room for every sid of this user (multi-device).
        for u_sid in presence_service.get_sids(user_id):
            try:
                await sio.leave_room(u_sid, call_room)
            except Exception:
                pass

        await presence_service.set_status(user_id, "online")
        await sio.emit("presence:user_status", {"user_id": user_id, "status": "online"})

        # Update / clear cross-server signal authz shadow.
        from app.services.call_signal_authz import call_signal_authz
        if call.status == "ended":
            call_signal_authz.clear(call_id)

            # ── Active-call channel broadcast (Join Existing Call UX) ────
            # Tell every channel member — even those off-server — that
            # the live call has ended so the "Join Call" affordance can
            # be removed from the UI without polling.
            _ended_channel_id = call.channel_id or data.get("channel_id")
            if _ended_channel_id:
                _channel_ended_payload = {
                    "channel_id": _ended_channel_id,
                    "call_id": call_id,
                    "ended_by": user_id,
                    "reason": "all_participants_left",
                }
                try:
                    from app.socket.channel_room import (
                        room_name as _chan_room_name_end,
                    )
                    await sio.emit(
                        "channel:active_call_ended",
                        _channel_ended_payload,
                        room=_chan_room_name_end(_ended_channel_id),
                    )
                except Exception as _e_ace:
                    logger.warning(
                        "active_call_end_broadcast_local_failed",
                        channel_id=_ended_channel_id, error=str(_e_ace),
                    )

                async def _broadcast_active_call_ended_remote() -> None:
                    try:
                        from sqlalchemy import select as _sel_ace
                        from app.models.channel import ChannelMember as _CM_ace
                        async with async_session_factory() as _db_ace:
                            rows = (await _db_ace.execute(
                                _sel_ace(_CM_ace.user_id).where(
                                    _CM_ace.channel_id == _ended_channel_id
                                )
                            )).all()
                        member_ids = [r[0] for r in rows]
                        import asyncio as _asyncio_ace
                        await _asyncio_ace.gather(
                            *(emit_to_user(
                                "channel:active_call_ended",
                                _channel_ended_payload,
                                mid,
                            ) for mid in member_ids
                              if not presence_service.get_sids(mid)),
                            return_exceptions=True,
                        )
                    except Exception as _e_ace2:
                        logger.debug(
                            "active_call_end_broadcast_remote_failed",
                            channel_id=_ended_channel_id, error=str(_e_ace2),
                        )

                import asyncio as _asyncio_ace_outer
                _end_bcast_task = _asyncio_ace_outer.create_task(
                    _broadcast_active_call_ended_remote()
                )
                _end_bcast_task.set_name(f"active_call_ended:{call_id}")
                try:
                    from app.services.call_service import call_service as _cs_end
                    _cs_end._bg_tasks.add(_end_bcast_task)
                    _end_bcast_task.add_done_callback(_cs_end._bg_tasks.discard)
                except Exception:
                    pass

            async def _persist_log():
                try:
                    async with async_session_factory() as db:
                        await call_service.persist_call_log(db, call)
                except Exception as exc:
                    logger.error("v2_leave_persist_failed", call_id=call_id, error=str(exc))
            import asyncio as _a
            _a.create_task(_persist_log())
        else:
            call_signal_authz.remove_participant(call_id, user_id)

        return {"status": "left"}

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def v2_call_toggle_mute(sid: str, data: dict):
    """V2 — Toggle mute and broadcast participant state."""
    user_id = await get_user_id(sid)
    if not user_id:
        return

    muted = data.get("muted", False)
    call = await call_service.toggle_mute(user_id, muted)

    if call:
        await _broadcast_participant_state(call, user_id)


@sio.event
async def v2_call_toggle_video(sid: str, data: dict):
    """V2 — Toggle video and broadcast participant state."""
    user_id = await get_user_id(sid)
    if not user_id:
        return

    video_off = data.get("video_off", False)
    call = await call_service.toggle_video(user_id, video_off)

    if call:
        await _broadcast_participant_state(call, user_id)


@sio.event
async def v2_call_screen_share_start(sid: str, data: dict):
    """V2 — Screen share start and broadcast participant state."""
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call = await call_service.toggle_screen_share(user_id, True)

    if call:
        await _broadcast_participant_state(call, user_id)


@sio.event
async def v2_call_screen_share_stop(sid: str, data: dict):
    """V2 — Screen share stop and broadcast participant state."""
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call = await call_service.toggle_screen_share(user_id, False)

    if call:
        await _broadcast_participant_state(call, user_id)


@sio.event
async def v2_call_host_force_all(sid: str, data: dict):
    """V2 — Host fan-out moderation action across the whole call.

    Payload: ``{call_id, action, except_self?, except_user_ids?}``
    where ``action`` is one of:
      * ``mute``         — force everyone's audio muted
      * ``unmute``       — request everyone unmute (clients
                           interpret as a soft "you may unmute" prompt)
      * ``video_off``    — force everyone's video off
      * ``video_on``     — request everyone enable video

    Only the host (or a designated co-host) can invoke. Targets can
    be filtered with ``except_self=true`` (default) and an explicit
    ``except_user_ids`` list (e.g. preserve speakers in a panel).
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    action = data.get("action")
    if not call_id or action not in ("mute", "unmute", "video_off", "video_on"):
        return
    call = call_service.get_call(call_id)
    if not call:
        return
    # Authorisation: host OR a registered co-host (see co-host
    # service below). For now we allow only the initiator; the
    # co-host check is layered on once that service is configured.
    co_hosts = getattr(call, "co_hosts", set()) or set()
    if call.initiator_id != user_id and user_id not in co_hosts:
        return

    except_self = bool(data.get("except_self", True))
    except_user_ids = set(data.get("except_user_ids") or [])
    if except_self:
        except_user_ids.add(user_id)

    targets = [
        uid for uid in call.participants.keys()
        if uid not in except_user_ids
    ]

    # Apply server-side authoritative state for hard mutes/video-offs
    # so the participant tile UI reflects reality even if a stale
    # client doesn't respond. Soft "unmute"/"video_on" is just a
    # prompt — we don't flip flags for those.
    if action in ("mute", "video_off"):
        flag = "muted" if action == "mute" else "video_off"
        for uid in targets:
            try:
                if action == "mute":
                    await call_service.toggle_mute(uid, True)
                else:
                    await call_service.toggle_video(uid, True)
            except Exception:
                pass

    payload = {
        "call_id": call_id,
        "action": action,
        "by_user_id": user_id,
        "ts": int(time.time() * 1000),
    }
    # Per-user emit so each client knows whether it was targeted
    # (and so we can ALSO carry a flag for "you were excluded" in
    # future). For now we just fan out to targets.
    for uid in targets:
        for usid in presence_service.get_sids(uid) or []:
            try:
                await sio.emit("call:host_force", payload, to=usid)
            except Exception:
                pass


# ── Co-host management ─────────────────────────────────────────────

@sio.event
async def v2_call_cohost_add(sid: str, data: dict):
    """V2 — Host promotes a participant to co-host."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    target = data.get("user_id")
    if not call_id or not target:
        return
    call = call_service.get_call(call_id)
    if not call or call.initiator_id != user_id:
        return
    if target not in call.participants:
        return
    co_hosts = getattr(call, "co_hosts", None)
    if co_hosts is None:
        co_hosts = set()
        call.co_hosts = co_hosts  # type: ignore[attr-defined]
    co_hosts.add(target)
    await sio.emit(
        "call:cohost_changed",
        {"call_id": call_id, "user_id": target, "is_cohost": True},
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_cohost_remove(sid: str, data: dict):
    """V2 — Host demotes a co-host."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    target = data.get("user_id")
    if not call_id or not target:
        return
    call = call_service.get_call(call_id)
    if not call or call.initiator_id != user_id:
        return
    co_hosts = getattr(call, "co_hosts", None)
    if not co_hosts:
        return
    co_hosts.discard(target)
    await sio.emit(
        "call:cohost_changed",
        {"call_id": call_id, "user_id": target, "is_cohost": False},
        room=f"call:{call_id}",
    )


# ── Watch party (synchronized playback) ────────────────────────────

@sio.event
async def v2_call_watchparty_start(sid: str, data: dict):
    """V2 — Host starts a watch-party session.

    Payload: ``{call_id, source_url, started_at_ms?}``.
    All clients open the URL in a synchronized player and rely on
    subsequent ``v2_call_watchparty_state`` events for play/pause/seek.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    source_url = (data.get("source_url") or "").strip()
    if not call_id or not source_url:
        return
    call = call_service.get_call(call_id)
    if not call or call.initiator_id != user_id:
        return
    await sio.emit(
        "call:watchparty_started",
        {
            "call_id": call_id,
            "source_url": source_url,
            "started_at_ms": int(time.time() * 1000),
            "by": user_id,
        },
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_watchparty_state(sid: str, data: dict):
    """V2 — Host (or co-host) ticks the playhead so others sync.

    Payload: ``{call_id, playing, position_ms, ts}``. Other clients
    soft-correct their local playhead toward this state with a small
    deadband (~250ms) to avoid stutter on tiny clock drift.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    playing = bool(data.get("playing", True))
    position_ms = int(data.get("position_ms") or 0)
    if not call_id:
        return
    call = call_service.get_call(call_id)
    if not call:
        return
    co_hosts = getattr(call, "co_hosts", set()) or set()
    if call.initiator_id != user_id and user_id not in co_hosts:
        return
    await sio.emit(
        "call:watchparty_state",
        {
            "call_id": call_id,
            "playing": playing,
            "position_ms": position_ms,
            "ts": int(time.time() * 1000),
        },
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_watchparty_stop(sid: str, data: dict):
    """V2 — Host ends the watch party."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    if not call_id:
        return
    call = call_service.get_call(call_id)
    if not call or call.initiator_id != user_id:
        return
    await sio.emit(
        "call:watchparty_stopped",
        {"call_id": call_id},
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_breakout_open(sid: str, data: dict):
    """V2 — Host opens breakout rooms.

    Payload: ``{call_id, groups: [{id, name, members:[user_id...]}]}``.

    Each participant gets a personalised ``call:breakout_assigned``
    so their client knows which group to join. Other participants
    of the call (those not assigned anywhere) get
    ``call:breakout_state`` for awareness.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    groups = data.get("groups") or []
    if not call_id or not isinstance(groups, list):
        return
    ok = await call_service.open_breakouts(call_id, user_id, groups)
    if not ok:
        return

    info = call_service.get_breakouts(call_id)

    # Per-user assignment notifications.
    for uid, gid in info["assignments"].items():
        for usid in presence_service.get_sids(uid) or []:
            try:
                await sio.emit(
                    "call:breakout_assigned",
                    {"call_id": call_id, "group_id": gid},
                    to=usid,
                )
            except Exception:
                pass
    # Broadcast public state (group names, who's where) so the host
    # UI + everyone's roster shows the partition.
    await sio.emit(
        "call:breakout_state",
        {"call_id": call_id, **info, "open": True},
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_breakout_close(sid: str, data: dict):
    """V2 — Host closes breakouts; everyone rejoins the main mesh."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    if not call_id:
        return
    ok = await call_service.close_breakouts(call_id, user_id)
    if not ok:
        return
    await sio.emit(
        "call:breakout_state",
        {"call_id": call_id, "groups": [], "assignments": {}, "open": False},
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_whisper(sid: str, data: dict):
    """V2 — Private text whisper from a participant to the host (or
    from the host to a single participant).

    Routing rules:
      * Sender = participant, target unspecified  →  delivered to
        every connected sid of the call's host.
      * Sender = host, target_user_id specified   →  delivered to
        every sid of that user.
      * Anything else (whisper between two non-hosts, or whisper
        targeting someone outside the call) is dropped silently.

    The whisper bypasses the room emit, so other participants don't
    see it. Server doesn't persist the message — whispers are
    transient by design.

    Payload: ``{call_id, text, target_user_id?}``.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    text = (data.get("text") or "").strip()
    target_user_id = data.get("target_user_id")
    if not call_id or not text or len(text) > 800:
        return

    call = call_service.get_call(call_id)
    if not call or user_id not in call.participants:
        return

    is_host = call.initiator_id == user_id
    if is_host:
        if not target_user_id or target_user_id not in call.participants:
            return
        recipients = [target_user_id]
    else:
        # Non-host whispers always go to the host.
        recipients = [call.initiator_id]
        target_user_id = call.initiator_id

    payload = {
        "call_id": call_id,
        "from_user_id": user_id,
        "to_user_id": target_user_id,
        "text": text,
        "ts": int(time.time() * 1000),
        "from_host": is_host,
    }

    # Deliver to every sid of every recipient.
    for rid in recipients:
        for rsid in presence_service.get_sids(rid) or []:
            try:
                await sio.emit("call:whisper", payload, to=rsid)
            except Exception:
                pass

    # Echo to the sender so their UI shows the line in their own
    # whisper history without a round-trip via the participant emit.
    try:
        await sio.emit("call:whisper", payload, to=sid)
    except Exception:
        pass


@sio.event
async def v2_call_passcode_set(sid: str, data: dict):
    """V2 — Host sets/clears a passcode for the call.

    Empty passcode disables the gate. Hash is computed server-side
    so the plain PIN never lives in the DB. The state change is
    broadcast as a boolean ("locked" or not) — clients DON'T receive
    the hash.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    plain = (data.get("passcode") or "").strip()
    if not call_id:
        return
    ok = await call_service.set_call_passcode(call_id, plain, user_id)
    if not ok:
        return
    await sio.emit(
        "call:passcode_state",
        {"call_id": call_id, "locked": bool(plain)},
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_passcode_verify(sid: str, data: dict):
    """V2 — Joiner submits a passcode for verification.

    Returns the result via a direct ``call:passcode_verify_result``
    emit to the requesting sid; the joiner then proceeds with the
    normal join flow on success.

    The passcode is rate-limited per-sid: 5 attempts per 30 seconds
    to make brute-force impractical. State lives in a module-level
    bucket; a misbehaving client gets `rate_limited` after that.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    plain = data.get("passcode") or ""
    if not call_id:
        return

    # Per-sid rate limit. Bucket survives the call but is small.
    global _PASSCODE_ATTEMPTS
    try:
        bucket = _PASSCODE_ATTEMPTS
    except NameError:
        bucket = {}
        globals()["_PASSCODE_ATTEMPTS"] = bucket
    now = time.time()
    history = [t for t in bucket.get(sid, []) if now - t < 30]
    if len(history) >= 5:
        await sio.emit(
            "call:passcode_verify_result",
            {"call_id": call_id, "ok": False, "reason": "rate_limited"},
            to=sid,
        )
        bucket[sid] = history
        return
    history.append(now)
    bucket[sid] = history

    ok = call_service.verify_passcode(call_id, plain)
    await sio.emit(
        "call:passcode_verify_result",
        {"call_id": call_id, "ok": bool(ok)},
        to=sid,
    )


@sio.event
async def v2_call_qa_ask(sid: str, data: dict):
    """V2 — Audience submits a Q&A question.

    Webinar feature parallel to chat: questions go to a separate
    queue the host can mark as answered. Each question is broadcast
    to every participant (so everyone sees what's been asked +
    what's been answered).

    Payload: ``{call_id, text}``. Server stamps ``id`` + ``ts``.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    text = (data.get("text") or "").strip()
    if not call_id or not text or len(text) > 800:
        return
    call = call_service.get_call(call_id)
    if not call or user_id not in call.participants:
        return
    qid = f"qa-{int(time.time() * 1000)}-{user_id[:6]}"
    payload = {
        "call_id": call_id,
        "id": qid,
        "user_id": user_id,
        "text": text,
        "ts": int(time.time() * 1000),
        "status": "open",   # open | answered | dismissed
    }
    await sio.emit("call:qa_added", payload, room=f"call:{call_id}")


@sio.event
async def v2_call_qa_resolve(sid: str, data: dict):
    """V2 — Host marks a Q&A question as answered or dismissed."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    qid = data.get("id")
    new_status = data.get("status")
    if not call_id or not qid or new_status not in ("answered", "dismissed", "open"):
        return
    call = call_service.get_call(call_id)
    if not call or call.initiator_id != user_id:
        return
    await sio.emit(
        "call:qa_status",
        {"call_id": call_id, "id": qid, "status": new_status},
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_qa_upvote(sid: str, data: dict):
    """V2 — Participant upvotes a question; helps the host pick which
    to answer first."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    qid = data.get("id")
    delta = 1 if data.get("up", True) else -1
    if not call_id or not qid:
        return
    call = call_service.get_call(call_id)
    if not call or user_id not in call.participants:
        return
    await sio.emit(
        "call:qa_vote",
        {
            "call_id": call_id, "id": qid,
            "user_id": user_id, "delta": delta,
        },
        room=f"call:{call_id}",
    )


# ── In-call polls ─────────────────────────────────────────────────

@sio.event
async def v2_call_poll_create(sid: str, data: dict):
    """V2 — Host launches a quick poll. Single-choice multiple options."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    question = (data.get("question") or "").strip()
    options = data.get("options") or []
    if (
        not call_id or not question or len(question) > 400
        or not isinstance(options, list)
        or not (2 <= len(options) <= 8)
    ):
        return
    options = [str(o).strip()[:120] for o in options if str(o).strip()]
    if len(options) < 2:
        return
    call = call_service.get_call(call_id)
    if not call or call.initiator_id != user_id:
        return
    poll_id = f"poll-{int(time.time() * 1000)}"
    await sio.emit(
        "call:poll_started",
        {
            "call_id": call_id,
            "id": poll_id,
            "question": question,
            "options": options,
            "ts": int(time.time() * 1000),
        },
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_poll_vote(sid: str, data: dict):
    """V2 — Participant casts a vote. Identity is broadcast so other
    clients can show "you voted X" — disable secrecy by design here
    (LAN-only setting; users see each other anyway)."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    poll_id = data.get("id")
    choice = data.get("choice")
    if (
        not call_id or not poll_id
        or not isinstance(choice, int) or choice < 0
    ):
        return
    call = call_service.get_call(call_id)
    if not call or user_id not in call.participants:
        return
    await sio.emit(
        "call:poll_vote",
        {
            "call_id": call_id, "id": poll_id,
            "user_id": user_id, "choice": choice,
        },
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_poll_close(sid: str, data: dict):
    """V2 — Host closes a poll. The clients keep showing the result
    as a static card."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    poll_id = data.get("id")
    if not call_id or not poll_id:
        return
    call = call_service.get_call(call_id)
    if not call or call.initiator_id != user_id:
        return
    await sio.emit(
        "call:poll_closed",
        {"call_id": call_id, "id": poll_id},
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_lobby_set_enabled(sid: str, data: dict):
    """V2 — Host enables/disables lobby for a call.

    Only the call's host (or a channel moderator) may toggle. Other
    callers get a silent no-op so a misbehaving client can't trigger
    admin events.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    enabled = bool(data.get("enabled", False))
    if not call_id:
        return
    call = call_service.get_call(call_id)
    if not call or call.initiator_id != user_id:
        return
    await call_service.set_lobby_enabled(call_id, enabled)
    await sio.emit(
        "call:lobby_state",
        {"call_id": call_id, "enabled": enabled},
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_lobby_knock(sid: str, data: dict):
    """V2 — A user requests entry to a locked call.

    The user is parked in ``lobby_pending`` and the host's clients
    receive ``call:lobby_knock`` with the requester info. Hosts
    respond via ``v2_call_lobby_admit`` / ``v2_call_lobby_deny``.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    display_name = data.get("display_name")
    if not call_id:
        return
    result = await call_service.lobby_knock(call_id, user_id, display_name)
    if result == "queued":
        # Notify the host's room so the queue panel updates.
        call = call_service.get_call(call_id)
        host = call.initiator_id if call else None
        if host:
            for host_sid in presence_service.get_sids(host) or []:
                try:
                    await sio.emit(
                        "call:lobby_knock",
                        {
                            "call_id": call_id,
                            "user_id": user_id,
                            "display_name": display_name,
                        },
                        to=host_sid,
                    )
                except Exception:
                    pass
    # Echo result back to the knocker so their UI can show "waiting"
    # vs "joined immediately".
    await sio.emit("call:lobby_knock_ack",
                   {"call_id": call_id, "result": result}, to=sid)


@sio.event
async def v2_call_lobby_admit(sid: str, data: dict):
    """V2 — Host admits a knocking user. Triggers their join flow."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    target_id = data.get("user_id")
    if not call_id or not target_id:
        return
    call = call_service.get_call(call_id)
    if not call or call.initiator_id != user_id:
        return
    if not await call_service.lobby_admit(call_id, target_id):
        return
    # Tell the admitted user to start their join flow.
    for tsid in presence_service.get_sids(target_id) or []:
        try:
            await sio.emit(
                "call:lobby_admitted",
                {"call_id": call_id},
                to=tsid,
            )
        except Exception:
            pass


@sio.event
async def v2_call_lobby_deny(sid: str, data: dict):
    """V2 — Host denies a knocking user."""
    user_id = await get_user_id(sid)
    if not user_id:
        return
    call_id = data.get("call_id")
    target_id = data.get("user_id")
    if not call_id or not target_id:
        return
    call = call_service.get_call(call_id)
    if not call or call.initiator_id != user_id:
        return
    if not await call_service.lobby_deny(call_id, target_id):
        return
    for tsid in presence_service.get_sids(target_id) or []:
        try:
            await sio.emit(
                "call:lobby_denied",
                {"call_id": call_id},
                to=tsid,
            )
        except Exception:
            pass


@sio.event
async def v2_call_transcribe_chunk(sid: str, data: dict):
    """V2 — Live transcription chunk from a participant's microphone.

    The client buffers ~3s of audio with MediaRecorder and uploads
    each chunk as base64-encoded WebM/Opus. We hand the chunk to
    whisper.cpp (already wrapped by transcription.py for batch
    voice-message paths) and broadcast the resulting text as a
    ``call:caption`` event so every participant sees live captions
    overlaid on the speaker's tile.

    Failures are silent — captions are best-effort, not authoritative.
    The caller controls the frequency, so a misbehaving client can't
    flood whisper; the worker queue length serves as backpressure.

    Payload: ``{call_id, audio_b64, mime, chunk_id, started_at}``.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call_id = data.get("call_id")
    audio_b64 = data.get("audio_b64") or ""
    mime = data.get("mime") or "audio/webm"
    chunk_id = data.get("chunk_id")
    if not call_id or not audio_b64 or len(audio_b64) > 5_000_000:
        # 5MB cap on a single chunk — at 24kbps Opus that's ~28
        # minutes, well above any reasonable 3-second window.
        return

    call = call_service.get_call(call_id)
    if not call or user_id not in call.participants:
        return

    # Hand off to the transcription worker; don't block the socket
    # handler. The worker writes the chunk to /tmp, runs whisper-cli,
    # and emits the caption.
    import asyncio as _asyncio_tx
    _asyncio_tx.create_task(_run_live_transcribe(
        call_id=call_id, user_id=user_id, audio_b64=audio_b64,
        mime=mime, chunk_id=chunk_id,
    ))


async def _run_live_transcribe(
    *, call_id: str, user_id: str, audio_b64: str, mime: str,
    chunk_id: int | str | None,
) -> None:
    """Worker: decode chunk → whisper → broadcast caption.

    Runs in its own task so a slow whisper invocation doesn't stall
    the socket pump. Errors are logged but never re-raised to keep
    the audit log out of confidential audio data.
    """
    import base64
    import os as _os_tx
    import tempfile as _tempfile_tx
    try:
        from app.services.transcription import WhisperTranscriber
        whisper = WhisperTranscriber()
        if not whisper.is_available():
            return  # silently no-op when whisper isn't configured

        try:
            audio_bytes = base64.b64decode(audio_b64, validate=True)
        except Exception:
            return
        if len(audio_bytes) < 1024:
            return  # too small to be useful

        ext = ".webm"
        if "ogg" in mime: ext = ".ogg"
        elif "wav" in mime: ext = ".wav"
        elif "mp4" in mime or "m4a" in mime: ext = ".m4a"

        fd, path = _tempfile_tx.mkstemp(prefix="helen_caption_", suffix=ext)
        try:
            with _os_tx.fdopen(fd, "wb") as f:
                f.write(audio_bytes)
            transcript = await whisper.transcribe(
                path,
                source_id=f"call:{call_id}:user:{user_id}:chunk:{chunk_id}",
                source_kind="call_caption",
                max_seconds_wait=20,
            )
        finally:
            try: _os_tx.unlink(path)
            except OSError: pass

        text = (transcript.full_text or "").strip()
        if not text:
            return

        await sio.emit(
            "call:caption",
            {
                "call_id": call_id,
                "user_id": user_id,
                "chunk_id": chunk_id,
                "text": text,
                "language": transcript.language,
                "ts": int(time.time() * 1000),
            },
            room=f"call:{call_id}",
        )
    except Exception as exc:
        logger.warning(
            "live_transcribe_failed",
            call_id=call_id, user_id=user_id, error=str(exc),
        )


@sio.event
async def v2_call_reaction(sid: str, data: dict):
    """V2 — Live in-call reaction (emoji float-up).

    Cheap fan-out — we don't persist reactions or rate-limit them
    server-side beyond the standard socket throttle. The reaction
    floats up the receiver's screen and is gone in ~2 seconds.

    Payload: ``{call_id, emoji}`` where emoji is a single character
    or short cluster (e.g. "👍", "🎉"). We trust the client to send
    valid emoji; rendering anything weird is a client UX problem,
    not a security one (the chat path is the audited content path).
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call_id = data.get("call_id")
    emoji = (data.get("emoji") or "").strip()
    if not call_id or not emoji or len(emoji) > 16:
        return

    call = call_service.get_call(call_id)
    if not call or user_id not in call.participants:
        return

    payload = {
        "call_id": call_id,
        "user_id": user_id,
        "emoji": emoji,
        "ts": int(time.time() * 1000),
    }
    await sio.emit(
        "call:reaction",
        payload,
        room=f"call:{call_id}",
    )


@sio.event
async def v2_call_toggle_hand(sid: str, data: dict):
    """V2 — Raise/lower hand and broadcast to all call participants.

    Webinar/large-meeting feature. Audience members raise their hand
    to ask a question; host sees the indicator (and the order in which
    hands were raised) so they can grant the floor.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    raised = bool(data.get("raised", False))
    call = await call_service.toggle_hand(user_id, raised)

    if call:
        p_data = call.participants.get(user_id, {})
        payload = {
            "call_id": call.call_id,
            "user_id": user_id,
            "raised": p_data.get("hand_raised", False),
            "raised_at": p_data.get("hand_raised_at"),
        }
        await sio.emit(
            "call:hand-changed",
            payload,
            room=f"call:{call.call_id}",
        )


async def _broadcast_participant_state(call, user_id: str) -> None:
    """
    Broadcast a unified call_participant_state event to all other participants
    via a single room emit (O(1) handler work).
    """
    p_data = call.participants.get(user_id, {})
    call_room = f"call:{call.call_id}"
    payload = {
        "call_id": call.call_id,
        "user_id": user_id,
        "muted": p_data.get("muted", False),
        "video_off": p_data.get("video_off", False),
        "sharing_screen": p_data.get("sharing_screen", False),
    }

    # Fast path: route per-user state flips through the coalescer.
    # Tight-burst flips (mute→unmute→mute during a debate) collapse
    # to a single latest-payload emit per 100ms window. The wire
    # event name stays canonical (``call_participant_state``); only
    # the coalesce key embeds the user_id so different users don't
    # shadow each other. When the coalescer isn't wired (e.g. test
    # harness) we fall back to the direct emit below.
    try:
        from app.services.broadcast_coalescer import get_broadcast_coalescer
        coalescer = get_broadcast_coalescer()
        if coalescer is not None:
            await coalescer.submit(
                call_id=call.call_id,
                event="call_participant_state",
                payload=payload,
                room=call_room,
                coalesce_key=f"state:{user_id}",
            )
            return
    except Exception:
        pass
    # Skip ALL sids of the origin user so they don't echo-receive their own
    # state on any of their connected devices. Socket.IO's `skip_sid` only
    # accepts a single sid, so for multi-device users we fan out per-sid:
    # collect every sid in the call_room except the origin's, then emit
    # individually. This costs O(participants × devices) emits but is the
    # only way to guarantee the origin user — across desktop, phone, tablet
    # — never sees their own state echoed back, which previously caused
    # double-toggle and audio/video desync on multi-device setups.
    origin_sids = set(presence_service.get_sids(user_id))
    if origin_sids:
        # Pull the room's sid set and exclude every origin sid.
        room_sids = set(sio.manager.rooms.get("/", {}).get(call_room, set()))
        targets = room_sids - origin_sids
        if targets:
            import asyncio as _asyncio_state
            await _asyncio_state.gather(
                *(sio.emit("call_participant_state", payload, to=sid) for sid in targets),
                return_exceptions=True,
            )
    else:
        await sio.emit("call_participant_state", payload, room=call_room)

    # Cross-server fanout: peers hosted on a sibling Helen server aren't
    # in this server's call_room, so the room broadcast misses them.
    # emit_to_user routes to local sids OR falls back to federation —
    # we only invoke it for participants WITHOUT local sids so we don't
    # double-deliver to co-located peers.
    import asyncio as _asyncio_xs
    for pid in list(call.participants.keys()):
        if pid == user_id:
            continue
        if presence_service.get_sids(pid):
            continue  # local — already covered by the room emit above
        try:
            _asyncio_xs.create_task(emit_to_user(
                "call_participant_state", payload, pid,
            ))
        except Exception as _e:
            logger.debug("cross_server_state_fanout_failed",
                         user_id=pid, error=str(_e))


# ── Call Hold/Resume Events ────────────────────────────────

@sio.event
async def call_hold(sid: str, data: dict):
    """
    Place a call on hold.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    try:
        result = await call_service.hold_call(call_id, user_id)
        call = call_service.get_call(call_id)

        # Notify other participants about hold state. emit_to_user
        # delivers to every local sid AND falls back to federation for
        # cross-server participants — gone is the room/per-sid split.
        for pid in call.participants:
            if pid != user_id:
                await emit_to_user("call_hold_state", {
                    "call_id": call_id,
                    "user_id": user_id,
                    "on_hold": True,
                }, pid)

        return result

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def call_resume(sid: str, data: dict):
    """
    Resume a held call.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    try:
        result = await call_service.resume_call(call_id, user_id)
        call = call_service.get_call(call_id)

        # Notify other participants about resume state. emit_to_user
        # delivers locally AND across federation for remote participants.
        for pid in call.participants:
            if pid != user_id:
                await emit_to_user("call_hold_state", {
                    "call_id": call_id,
                    "user_id": user_id,
                    "on_hold": False,
                }, pid)

        return result

    except ValueError as e:
        return {"error": str(e)}


# ── Moderation Events (host / channel-moderator) ──────────────────
#
# Three events let the host (or a channel admin/moderator) enforce
# basic moderation primitives over a live group call:
#
#   call_kick_participant      — remove a single user from the call
#   call_force_mute            — toggle a user's mute state without consent
#   call_end_for_everyone      — end the call for all participants
#
# Authorization: caller must be EITHER the call's host (initiator_id)
# OR hold an admin/moderator role in the call's backing channel.
# Authorization is checked SERVER-SIDE — the UI button is a hint, not
# a security boundary.


async def _is_call_moderator(call, user_id: str) -> bool:
    """Return True if user_id is the call's host OR holds an admin /
    moderator role in the call's channel. Pure read helper; never
    raises — returns False on any internal error."""
    if call is None:
        return False
    if call.initiator_id == user_id:
        return True
    if not getattr(call, "channel_id", None):
        return False
    try:
        async with async_session_factory() as db:
            from app.services.channel_service import ChannelService as _CS
            member = await _CS._get_member(db, call.channel_id, user_id)
            return getattr(member, "role", "") in ("admin", "moderator")
    except Exception:
        return False


@sio.event
async def call_kick_participant(sid: str, data: dict):
    """
    Remove a participant from a live call.
    data: { call_id: str, target_user_id: str }
    Authorization: host or channel admin/moderator.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    target_id = data.get("target_user_id")
    if not call_id or not target_id:
        return {"error": "call_id and target_user_id required"}

    call = call_service.get_call(call_id)
    if not call:
        return {"error": "Call not found"}

    if not await _is_call_moderator(call, user_id):
        from app.core.audit import audit_call_signal_unauthorized
        audit_call_signal_unauthorized(user_id, target_id, "call_kick_unauthorized")
        return {"error": "forbidden"}

    if target_id not in call.participants:
        return {"error": "target_not_in_call"}

    if target_id == call.initiator_id and target_id != user_id:
        # Host may kick anyone (including themselves via end-for-everyone).
        # Moderators cannot kick the host — that's host's prerogative.
        if call.initiator_id != user_id:
            return {"error": "cannot_kick_host"}

    try:
        ended_call = await call_service.leave_call(call_id, target_id)
    except Exception as e:
        logger.error("call_kick_failed", call_id=call_id, target=target_id, error=str(e))
        return {"error": "kick_failed", "detail": str(e)}

    # Tell the kicked user directly so their UI can tear down its
    # PeerConnection cleanly. Cross-server safe via emit_to_user.
    await emit_to_user("call:kicked", {
        "call_id": call_id,
        "by": user_id,
        "reason": data.get("reason") or "kicked_by_moderator",
    }, target_id)

    # Tell remaining participants the user left.
    call_room = f"call:{call_id}"
    await sio.emit("call_participant_left", {
        "call_id": call_id,
        "user_id": target_id,
        "reason": "kicked",
        "by": user_id,
    }, room=call_room)

    # Cross-server fanout for remote participants.
    for pid in list(ended_call.participants.keys()):
        if pid == target_id or pid == user_id:
            continue
        if presence_service.get_sids(pid):
            continue
        try:
            await emit_to_user("call_participant_left", {
                "call_id": call_id,
                "user_id": target_id,
                "reason": "kicked",
                "by": user_id,
            }, pid)
        except Exception:
            pass

    # Authz shadow eviction so the kicked user can't keep relaying signals.
    try:
        from app.services.call_signal_authz import call_signal_authz
        if ended_call.status == "ended":
            call_signal_authz.clear(call_id)
        else:
            call_signal_authz.remove_participant(call_id, target_id)
    except Exception:
        pass

    logger.info("call_kick_ok", call_id=call_id, target=target_id, by=user_id)
    return {"status": "kicked", "target_user_id": target_id}


@sio.event
async def call_force_mute(sid: str, data: dict):
    """
    Force-set a participant's mute state.
    data: { call_id: str, target_user_id: str, muted: bool }
    Authorization: host or channel admin/moderator.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    target_id = data.get("target_user_id")
    muted = bool(data.get("muted", True))
    if not call_id or not target_id:
        return {"error": "call_id and target_user_id required"}

    call = call_service.get_call(call_id)
    if not call:
        return {"error": "Call not found"}

    if not await _is_call_moderator(call, user_id):
        return {"error": "forbidden"}

    if target_id not in call.participants:
        return {"error": "target_not_in_call"}

    try:
        await call_service.toggle_mute(target_id, muted)
    except Exception as e:
        logger.error("call_force_mute_failed",
                     call_id=call_id, target=target_id, error=str(e))
        return {"error": "force_mute_failed", "detail": str(e)}

    # Tell the muted user so their UI can flip their local mic state
    # AND show a "you were muted by moderator" toast.
    await emit_to_user("call:force_muted", {
        "call_id": call_id,
        "muted": muted,
        "by": user_id,
    }, target_id)

    # Broadcast new participant state so every client's UI updates.
    refreshed = call_service.get_call(call_id)
    if refreshed is not None:
        await _broadcast_participant_state(refreshed, target_id)

    logger.info("call_force_mute_ok",
                call_id=call_id, target=target_id, by=user_id, muted=muted)
    return {"status": "ok", "target_user_id": target_id, "muted": muted}


@sio.event
async def call_end_for_everyone(sid: str, data: dict):
    """
    End the call for ALL participants. HOST ONLY (not moderators) —
    moderators can kick individuals but only the host can terminate
    the entire call.
    data: { call_id: str, reason?: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    call = call_service.get_call(call_id)
    if not call:
        return {"error": "Call not found"}

    # Host only — channel mods don't get to terminate the host's call.
    if call.initiator_id != user_id:
        from app.core.audit import audit_call_signal_unauthorized
        audit_call_signal_unauthorized(user_id, call_id, "call_end_for_everyone_unauthorized")
        return {"error": "forbidden_only_host"}

    reason = data.get("reason") or "host_ended_for_everyone"

    # Notify every participant before tearing down state.
    for pid in list(call.participants.keys()):
        if pid != user_id:
            try:
                await emit_to_user("call_hangup", {
                    "call_id": call_id,
                    "ended_by": user_id,
                    "reason": reason,
                }, pid)
            except Exception:
                pass

    try:
        ended = await call_service.hangup(call_id, user_id)
    except Exception as e:
        logger.error("call_end_for_everyone_failed", call_id=call_id, error=str(e))
        return {"error": "hangup_failed", "detail": str(e)}

    # Drop authz shadow + persist log.
    try:
        from app.services.call_signal_authz import call_signal_authz
        call_signal_authz.clear(call_id)
    except Exception:
        pass
    try:
        async with async_session_factory() as db:
            await call_service.persist_call_log(db, ended)
    except Exception as e:
        logger.warning("end_for_everyone_persist_failed", error=str(e))

    logger.info("call_end_for_everyone_ok", call_id=call_id, by=user_id)
    return {"status": "ended"}


# ── Call Quality Report Events ────────────────────────────

@sio.event
async def call_quality_report(sid: str, data: dict):
    """
    Report call quality metrics.
    data: {
        call_id: str,
        metrics: {
            rtt_ms?: float,           # round trip time
            packet_loss?: float,      # 0-1 (percentage)
            bandwidth_mbps?: float,
            codec?: str,
            audio_level?: float,
            video_fps?: int,
            video_resolution?: str,
        }
    }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    metrics = data.get("metrics", {})

    if not call_id:
        return {"error": "call_id is required"}

    try:
        await call_service.report_quality(call_id, user_id, metrics)
        return {"status": "reported"}
    except ValueError as e:
        return {"error": str(e)}


# ── Call Transfer Events ───────────────────────────────────

@sio.event
async def call_transfer_request(sid: str, data: dict):
    """
    Request to transfer a call to another user.
    data: { call_id: str, target_user_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    target_user_id = data.get("target_user_id")

    if not call_id or not target_user_id:
        return {"error": "call_id and target_user_id are required"}

    try:
        call = call_service.get_call(call_id)
        if not call:
            return {"error": "Call not found"}

        # Notify target user about transfer request
        for target_sid in presence_service.get_sids(target_user_id):
            await sio.emit("call_transfer_request", {
                "call_id": call_id,
                "from_user": user_id,
                "target_user": target_user_id,
            }, to=target_sid)

        return {"status": "transfer_requested"}

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def call_transfer_accept(sid: str, data: dict):
    """
    Accept an incoming call transfer.
    data: { call_id: str, from_user: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    from_user = data.get("from_user")

    if not call_id or not from_user:
        return {"error": "call_id and from_user are required"}

    try:
        result = await call_service.transfer_call(call_id, from_user, user_id)
        call = call_service.get_call(call_id)

        # Notify remaining participants about transfer
        for pid in call.participants:
            for p_sid in presence_service.get_sids(pid):
                await sio.emit("call_transfer_accepted", {
                    "call_id": call_id,
                    "from_user": from_user,
                    "to_user": user_id,
                }, to=p_sid)

        return result

    except ValueError as e:
        return {"error": str(e)}


@sio.event
async def call_transfer_reject(sid: str, data: dict):
    """
    Reject an incoming call transfer.
    data: { call_id: str, from_user: str, reason?: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    from_user = data.get("from_user")
    reason = data.get("reason", "rejected")

    if not call_id or not from_user:
        return {"error": "call_id and from_user are required"}

    try:
        # Notify transferring user about rejection
        for from_sid in presence_service.get_sids(from_user):
            await sio.emit("call_transfer_rejected", {
                "call_id": call_id,
                "target_user": user_id,
                "reason": reason,
            }, to=from_sid)

        return {"status": "transfer_rejected"}

    except Exception as e:
        logger.error("call_transfer_reject_error", error=str(e))
        return {"error": str(e)}


# ── Enhanced Call Info Event ───────────────────────────────

@sio.event
async def call_get_info(sid: str, data: dict):
    """
    Get detailed information about a call.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    try:
        call_details = await call_service.get_call_details(call_id)

        # Verify user is a participant
        if user_id not in [p["user_id"] for p in call_details.get("participants", [])]:
            logger.warning("call_get_info_unauthorized", user_id=user_id, call_id=call_id)
            return {"error": "Not authorized to view call details"}

        return call_details

    except ValueError as e:
        return {"error": str(e)}


# ── Network Quality Probe Event ────────────────────────────

@sio.event
async def call_network_probe(sid: str, data: dict):
    """
    Network quality probe — client sends timestamp, server echoes it back.
    Used by client to calculate round-trip latency.
    data: { call_id: str, client_timestamp_ms: float, sequence: int }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    client_timestamp_ms = data.get("client_timestamp_ms")
    sequence = data.get("sequence", 0)

    if not call_id or client_timestamp_ms is None:
        return {"error": "call_id and client_timestamp_ms are required"}

    try:
        # Verify user is in the call
        call = call_service.get_call(call_id)
        if not call or user_id not in call.participants:
            return {"error": "Not authorized"}

        # Echo back with server timestamp
        import time
        server_timestamp_ms = time.time() * 1000

        return {
            "call_id": call_id,
            "client_timestamp_ms": client_timestamp_ms,
            "server_timestamp_ms": server_timestamp_ms,
            "sequence": sequence,
        }

    except Exception as e:
        logger.error("call_network_probe_error", error=str(e))
        return {"error": str(e)}


# ── Bandwidth Estimation Event ────────────────────────────

@sio.on("network_probe")
async def _network_probe_alias(sid: str, data: dict):
    """Client emits 'network_probe' from CallEngine; delegate to call_network_probe."""
    return await call_network_probe(sid, data)


@sio.on("signal:offer")
async def _signal_offer_alias(sid: str, data: dict):
    return await signal_offer(sid, data)


@sio.on("signal:answer")
async def _signal_answer_alias(sid: str, data: dict):
    return await signal_answer(sid, data)


@sio.on("signal:ice_candidate")
async def _signal_ice_alias(sid: str, data: dict):
    return await signal_ice_candidate(sid, data)


# ── V2 Call Hold/Resume/Audio-Share ────────────────────────


@sio.event
async def v2_call_hold(sid: str, data: dict):
    """
    V2 — Place a call on hold.
    data: { call_id: str }
    """
    return await call_hold(sid, data)


@sio.event
async def v2_call_resume(sid: str, data: dict):
    """
    V2 — Resume a held call.
    data: { call_id: str }
    """
    return await call_resume(sid, data)


@sio.event
async def v2_call_screen_share_audio(sid: str, data: dict):
    """
    V2 — Toggle "share with system audio" while screen sharing.
    data: { call_id: str, has_audio: bool }
    Notifies all other participants so their UI updates.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    has_audio = bool(data.get("has_audio", False))

    if not call_id:
        return {"error": "call_id is required"}

    try:
        call = call_service.get_call(call_id)
        if not call or user_id not in call.participants:
            return {"error": "Not in call"}

        # Best-effort: stash on the participant dict if it's a mapping we own.
        try:
            p = call.participants.get(user_id, {})
            if isinstance(p, dict):
                p["screen_share_audio"] = has_audio
        except Exception:
            pass

        for pid in call.participants:
            if pid != user_id:
                for p_sid in presence_service.get_sids(pid):
                    await sio.emit(
                        "v2_call:screen_share_audio",
                        {
                            "call_id": call_id,
                            "user_id": user_id,
                            "has_audio": has_audio,
                        },
                        to=p_sid,
                    )

        return {"status": "ok", "has_audio": has_audio}

    except Exception as e:
        logger.error("v2_call_screen_share_audio_error", error=str(e), user_id=user_id)
        return {"error": str(e)}


@sio.event
async def call_bandwidth_test(sid: str, data: dict):
    """
    Bandwidth estimation test — client sends data payload, server measures and responds.
    data: {
        call_id: str,
        payload_size_bytes: int,
        sequence: int,
    }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    payload_size_bytes = data.get("payload_size_bytes", 1024)
    sequence = data.get("sequence", 0)

    if not call_id:
        return {"error": "call_id is required"}

    try:
        # Verify user is in the call
        call = call_service.get_call(call_id)
        if not call or user_id not in call.participants:
            return {"error": "Not authorized"}

        # Generate response payload of similar size to measure round-trip
        import time
        test_data = "x" * min(payload_size_bytes, 1024 * 100)  # Max 100KB per response
        server_timestamp_ms = time.time() * 1000

        return {
            "call_id": call_id,
            "sequence": sequence,
            "server_timestamp_ms": server_timestamp_ms,
            "payload_size_bytes": len(test_data),
            "payload": test_data,
        }

    except Exception as e:
        logger.error("call_bandwidth_test_error", error=str(e))
        return {"error": str(e)}


# ════════════════════════════════════════════════════════════════════════════
# SFU PROXY HANDLERS
# ════════════════════════════════════════════════════════════════════════════
# Proxy clients ↔ mediasoup worker through the Python server. The bridge is
# authenticated (we know who the user is via get_user_id). The mediasoup
# worker trusts the server by bearer token, so the worker never sees client
# tokens. This also lets us enforce call-membership on every action and fan
# out `call_sfu_new_producer` notifications to other peers.
#
# Event contract (client → server):
#   call_sfu_create_transport   { call_id, direction: "send"|"recv" }
#   call_sfu_connect_transport  { call_id, transport_id, dtls_parameters }
#   call_sfu_produce            { call_id, transport_id, kind, rtp_parameters, app_data? }
#   call_sfu_consume            { call_id, transport_id, producer_id, rtp_capabilities }
#   call_sfu_resume             { call_id, consumer_id }
#   call_sfu_pause              { call_id, consumer_id }
#
# Server → client broadcasts:
#   call_sfu_new_producer       { call_id, producer_id, peer_id, kind }
# ════════════════════════════════════════════════════════════════════════════


def _get_mediasoup_bridge():
    """Return the singleton MediasoupBridge if configured; raise otherwise."""
    from app.services.topology_manager import topology_manager, MediasoupBridge
    backend = topology_manager._backend
    if not isinstance(backend, MediasoupBridge):
        raise RuntimeError("sfu_not_configured")
    return backend


async def _require_call_participant(sid: str, call_id: str):
    """Resolve user + authorize membership. Returns (user_id, call) or raises."""
    user_id = await get_user_id(sid)
    if not user_id:
        raise PermissionError("unauthenticated")
    if not call_id:
        raise ValueError("missing call_id")
    call = call_service.get_call(call_id)
    if not call:
        raise ValueError("call_not_found")
    if user_id not in call.participants:
        raise PermissionError("not_a_participant")
    return user_id, call


async def _fanout_to_other_peers(
    call, event: str, payload: dict, origin_user_id: str,
) -> int:
    """Emit ``event`` to every sid of every participant except ``origin_user_id``.
    Returns the number of sid deliveries. Handles multi-device correctly."""
    total = 0
    for uid in list(call.participants.keys()):
        if uid == origin_user_id:
            continue
        try:
            total += await emit_to_user(event, payload, uid)
        except Exception as exc:
            logger.warning(
                "fanout_failed",
                event=event,
                user_id=uid,
                error=str(exc),
            )
    return total


@sio.event
async def call_sfu_create_transport(sid: str, data: dict):
    try:
        call_id = (data or {}).get("call_id")
        direction = (data or {}).get("direction", "recv")
        user_id, _ = await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        info = await bridge.create_transport(call_id, user_id, direction)
        return {"ok": True, **info}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_create_transport_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def call_sfu_connect_transport(sid: str, data: dict):
    try:
        call_id = (data or {}).get("call_id")
        transport_id = (data or {}).get("transport_id")
        dtls = (data or {}).get("dtls_parameters")
        if not transport_id or not dtls:
            return {"ok": False, "error": "missing transport_id|dtls_parameters"}
        await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        await bridge.connect_transport(call_id, transport_id, dtls)
        return {"ok": True}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_connect_transport_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def call_sfu_produce(sid: str, data: dict):
    try:
        call_id = (data or {}).get("call_id")
        transport_id = (data or {}).get("transport_id")
        kind = (data or {}).get("kind")
        rtp_parameters = (data or {}).get("rtp_parameters")
        app_data = (data or {}).get("app_data") or {}
        if not (transport_id and kind and rtp_parameters):
            return {"ok": False, "error": "missing transport_id|kind|rtp_parameters"}
        user_id, call = await _require_call_participant(sid, call_id)

        # ── Media policy enforcement ──
        # Clamp encoding.maxBitrate on the server side before handing off to
        # the SFU worker. Resolution + framerate are enforced client-side via
        # getUserMedia constraints (the client receives its cap via
        # /api/media-policy/me), so we only have bitrate to police here.
        if kind == "video":
            try:
                from app.services.media_policy_service import media_policy_service
                async with async_session_factory() as _db:
                    cap = await media_policy_service.effective_cap_for(_db, user_id)
                if cap.enforce_hard_cap and cap.max_bitrate_kbps > 0:
                    cap_bps = int(cap.max_bitrate_kbps) * 1000
                    encodings = rtp_parameters.get("encodings") or []
                    clamped_any = False
                    for enc in encodings:
                        mb = enc.get("maxBitrate")
                        if isinstance(mb, (int, float)) and mb > cap_bps:
                            enc["maxBitrate"] = cap_bps
                            clamped_any = True
                    if clamped_any:
                        logger.info(
                            "sfu_produce_bitrate_clamped",
                            user_id=user_id,
                            call_id=call_id,
                            cap_kbps=cap.max_bitrate_kbps,
                        )
            except Exception as _cap_err:
                # Never fail a call because of a policy read glitch.
                logger.warning(
                    "sfu_produce_cap_lookup_failed",
                    error=str(_cap_err),
                    user_id=user_id,
                )

        bridge = _get_mediasoup_bridge()
        result = await bridge.produce(
            call_id, transport_id, kind, rtp_parameters, app_data,
        )
        # Fan-out: notify every OTHER participant (all their devices) so they
        # can build a consumer for this new producer.
        producer_id = result.get("id")
        delivered = await _fanout_to_other_peers(
            call,
            "call_sfu_new_producer",
            {
                "call_id": call_id,
                "producer_id": producer_id,
                "peer_id": user_id,
                "kind": kind,
            },
            origin_user_id=user_id,
        )
        logger.debug(
            "sfu_new_producer_announced",
            call_id=call_id,
            producer_id=producer_id,
            peer_id=user_id,
            kind=kind,
            delivered=delivered,
        )
        return {"ok": True, **result}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_produce_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def call_sfu_consume(sid: str, data: dict):
    try:
        call_id = (data or {}).get("call_id")
        transport_id = (data or {}).get("transport_id")
        producer_id = (data or {}).get("producer_id")
        rtp_capabilities = (data or {}).get("rtp_capabilities")
        if not (transport_id and producer_id and rtp_capabilities):
            return {"ok": False, "error": "missing transport_id|producer_id|rtp_capabilities"}
        user_id, _ = await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        result = await bridge.consume(
            call_id, transport_id, producer_id, user_id, rtp_capabilities,
        )
        return {"ok": True, **result}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_consume_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def call_sfu_resume(sid: str, data: dict):
    try:
        call_id = (data or {}).get("call_id")
        consumer_id = (data or {}).get("consumer_id")
        if not consumer_id:
            return {"ok": False, "error": "missing consumer_id"}
        await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        await bridge.resume_consumer(call_id, consumer_id)
        return {"ok": True}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_resume_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def call_sfu_pause(sid: str, data: dict):
    try:
        call_id = (data or {}).get("call_id")
        consumer_id = (data or {}).get("consumer_id")
        if not consumer_id:
            return {"ok": False, "error": "missing consumer_id"}
        await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        await bridge.pause_consumer(call_id, consumer_id)
        return {"ok": True}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_pause_failed", error=str(e))
        return {"ok": False, "error": str(e)}


# ── SFU Producer Pause/Resume (wired to mute button) ───────────────────────

@sio.event
async def call_sfu_producer_pause(sid: str, data: dict):
    """
    Pause a producer on the SFU (wires mute → actual bandwidth saving).
    data: { call_id: str, producer_id: str }
    """
    try:
        call_id = (data or {}).get("call_id")
        producer_id = (data or {}).get("producer_id")
        if not producer_id:
            return {"ok": False, "error": "missing producer_id"}
        user_id, call = await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        await bridge.pause_producer(call_id, producer_id)
        # Fan-out so peers can grey out that video tile.
        await _fanout_to_other_peers(
            call,
            "call_sfu_producer_paused",
            {
                "call_id": call_id,
                "peer_id": user_id,
                "producer_id": producer_id,
            },
            origin_user_id=user_id,
        )
        return {"ok": True, "paused": True}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_producer_pause_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def call_sfu_producer_resume(sid: str, data: dict):
    """
    Resume a previously paused producer.
    data: { call_id: str, producer_id: str }
    """
    try:
        call_id = (data or {}).get("call_id")
        producer_id = (data or {}).get("producer_id")
        if not producer_id:
            return {"ok": False, "error": "missing producer_id"}
        user_id, call = await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        await bridge.resume_producer(call_id, producer_id)
        await _fanout_to_other_peers(
            call,
            "call_sfu_producer_resumed",
            {
                "call_id": call_id,
                "peer_id": user_id,
                "producer_id": producer_id,
            },
            origin_user_id=user_id,
        )
        return {"ok": True, "paused": False}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_producer_resume_failed", error=str(e))
        return {"ok": False, "error": str(e)}


# ── SFU Bandwidth / Simulcast Control ──────────────────────────────────────

@sio.event
async def call_sfu_set_preferred_layers(sid: str, data: dict):
    """
    Client → Server: choose simulcast / SVC layer for a consumer.

    Clients call this when their downlink bandwidth estimator decides that
    a lower spatial (resolution) or temporal (framerate) layer is a better
    fit for the current link.

    data: {
        call_id: str,
        consumer_id: str,
        spatial_layer: int,   # 0=low, 1=mid, 2=high (simulcast)
        temporal_layer?: int, # 0=base, 1/2=upper (SVC)
    }
    """
    try:
        call_id = (data or {}).get("call_id")
        consumer_id = (data or {}).get("consumer_id")
        spatial_layer = (data or {}).get("spatial_layer")
        temporal_layer = (data or {}).get("temporal_layer")
        if not consumer_id or spatial_layer is None:
            return {"ok": False, "error": "missing consumer_id|spatial_layer"}
        await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        await bridge.set_preferred_layers(
            call_id, consumer_id, int(spatial_layer),
            int(temporal_layer) if temporal_layer is not None else None,
        )
        return {"ok": True}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_set_preferred_layers_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def call_sfu_set_max_bitrate(sid: str, data: dict):
    """
    Set a per-transport incoming bitrate cap. Clients lower this when they
    detect they are on a metered/weak link.

    data: { call_id: str, transport_id: str, bitrate: int, direction: "incoming"|"outgoing" }
    """
    try:
        call_id = (data or {}).get("call_id")
        transport_id = (data or {}).get("transport_id")
        bitrate = (data or {}).get("bitrate")
        direction = (data or {}).get("direction", "incoming")
        if not transport_id or not bitrate:
            return {"ok": False, "error": "missing transport_id|bitrate"}
        await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        if direction == "outgoing":
            await bridge.set_max_outgoing_bitrate(call_id, transport_id, int(bitrate))
        else:
            await bridge.set_max_incoming_bitrate(call_id, transport_id, int(bitrate))
        return {"ok": True}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_set_max_bitrate_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def call_sfu_set_consumer_priority(sid: str, data: dict):
    """
    Raise or lower a consumer's bandwidth allocation priority.

    data: { call_id: str, consumer_id: str, priority: int (1..255) }
    """
    try:
        call_id = (data or {}).get("call_id")
        consumer_id = (data or {}).get("consumer_id")
        priority = (data or {}).get("priority")
        if not consumer_id or priority is None:
            return {"ok": False, "error": "missing consumer_id|priority"}
        await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        await bridge.set_consumer_priority(call_id, consumer_id, int(priority))
        return {"ok": True}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_set_consumer_priority_failed", error=str(e))
        return {"ok": False, "error": str(e)}


# ── SFU Active Speaker / Recording ─────────────────────────────────────────

@sio.event
async def call_sfu_attach_audio_producer(sid: str, data: dict):
    """
    Attach an audio producer to the router's AudioLevelObserver so the
    server-side worker starts emitting active_speaker events for it.

    data: { call_id: str, producer_id: str }
    """
    try:
        call_id = (data or {}).get("call_id")
        producer_id = (data or {}).get("producer_id")
        if not producer_id:
            return {"ok": False, "error": "missing producer_id"}
        await _require_call_participant(sid, call_id)
        bridge = _get_mediasoup_bridge()
        await bridge.ensure_audio_observer(call_id)
        await bridge.audio_observer_add(call_id, producer_id)
        return {"ok": True}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_attach_audio_producer_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def call_sfu_start_recording(sid: str, data: dict):
    """
    Start a server-side mix recording of the supplied producers.
    Only the call initiator can start a recording.

    data: {
        call_id: str,
        audio_producer_id?: str,
        video_producer_id?: str,
    }
    """
    try:
        call_id = (data or {}).get("call_id")
        user_id, call = await _require_call_participant(sid, call_id)
        if call.initiator_id != user_id:
            return {"ok": False, "error": "only the call initiator can start recording"}
        bridge = _get_mediasoup_bridge()
        result = await bridge.start_recording(
            call_id,
            audio_producer_id=(data or {}).get("audio_producer_id"),
            video_producer_id=(data or {}).get("video_producer_id"),
        )
        # Fan-out so every peer's UI shows the "recording" indicator.
        await _fanout_to_other_peers(
            call,
            "call_sfu_recording_started",
            {
                "call_id": call_id,
                "recording_id": result.get("recording_id"),
                "started_by": user_id,
            },
            origin_user_id=user_id,
        )
        return {"ok": True, **result}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_start_recording_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def call_sfu_stop_recording(sid: str, data: dict):
    """
    Stop a server-side recording. Only the initiator can stop.

    data: { call_id: str, recording_id: str }
    """
    try:
        call_id = (data or {}).get("call_id")
        recording_id = (data or {}).get("recording_id")
        if not recording_id:
            return {"ok": False, "error": "missing recording_id"}
        user_id, call = await _require_call_participant(sid, call_id)
        if call.initiator_id != user_id:
            return {"ok": False, "error": "only the call initiator can stop recording"}
        bridge = _get_mediasoup_bridge()
        result = await bridge.stop_recording(call_id, recording_id)
        await _fanout_to_other_peers(
            call,
            "call_sfu_recording_stopped",
            {
                "call_id": call_id,
                "recording_id": recording_id,
                "stopped_by": user_id,
                "output_path": result.get("output_path"),
            },
            origin_user_id=user_id,
        )
        return {"ok": True, **result}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error("sfu_stop_recording_failed", error=str(e))
        return {"ok": False, "error": str(e)}


# ── ICE / TURN fallback ─────────────────────────────────────


@sio.event
async def call_get_ice_servers(sid: str, data: dict | None = None):
    """
    On-demand ICE-servers refresh.

    Clients call this before their ephemeral TURN credentials expire
    (every ``ice_ttl_seconds`` minus some slack). Returns a payload
    identical in shape to what ``call:peer_ready`` carries.

    data: { call_id?: str, ttl_seconds?: int }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    ttl = None
    try:
        if data and data.get("ttl_seconds") is not None:
            ttl = int(data["ttl_seconds"])
    except Exception:
        ttl = None

    try:
        cfg = build_ice_config(user_id, ttl_seconds=ttl)
        return {
            "ok": True,
            "ice_servers": cfg["ice_servers"],
            "ice_transport_policy": cfg["ice_transport_policy"],
            "ice_ttl_seconds": cfg.get("ttl_seconds"),
            "realm": cfg.get("realm"),
        }
    except Exception as e:
        logger.error("call_get_ice_servers_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@sio.event
async def v2_call_get_ice_servers(sid: str, data: dict | None = None):
    """V2 alias of :func:`call_get_ice_servers`."""
    return await call_get_ice_servers(sid, data)
