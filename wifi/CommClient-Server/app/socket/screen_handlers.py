"""
Screen sharing and presenter management socket handlers.

These handlers manage the presenter lock for group screen sharing.
They work alongside the existing call handlers and the v2 screen share
events in call_handlers.py (v2_call_screen_share_start/stop).

Socket Events (client → server):
  presenter_request        — Request the presenter role
  presenter_release        — Release the presenter role
  presenter_cancel_request — Cancel a queued request
  presenter_force_stop     — Admin force-stops the current presenter

Socket Events (server → client):
  presenter_granted        — Presenter lock acquired
  presenter_released       — Presenter lock released
  presenter_force_stopped  — Presenter was force-stopped by admin
  presenter_queue_update   — Queue changed (position updates)
  presenter_promoted       — User promoted from queue to presenter
"""

from __future__ import annotations

import time

from app.core.logging import get_logger
from app.services.call_service import call_service
from app.services.presence_service import presence_service
from app.services.presenter_service import presenter_service
from app.socket.server import emit_to_user, get_user_id, sio

logger = get_logger(__name__)


@sio.event
async def presenter_request(sid: str, data: dict):
    """
    Request the presenter role for screen sharing.
    data: { call_id: str }
    Returns: { status: "granted" | "queued" | "denied", position?: int }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    # Validate call exists and user is a participant
    call = call_service.get_call(call_id)
    if not call:
        return {"error": "Call not found"}
    if user_id not in call.participants:
        return {"error": "Not a participant in this call"}

    # Get display name
    display_name = user_id  # Fallback

    result = presenter_service.request_presenter(call_id, user_id, display_name)

    # Log presenter request with result and queue position
    log_data = {
        "call_id": call_id,
        "user_id": user_id,
        "status": result["status"]
    }
    if result["status"] == "queued" and "position" in result:
        log_data["queue_position"] = result["position"]
    logger.info("presenter_request", **log_data)

    if result["status"] == "granted":
        # Notify all participants that this user is now the presenter
        for pid in call.participants:
            await emit_to_user("presenter_granted", {
                "call_id": call_id,
                "user_id": user_id,
                "display_name": display_name,
            }, pid)

    elif result["status"] == "queued":
        # Send queue update to all participants
        await _broadcast_queue_update(call_id, call)

    return result


@sio.event
async def presenter_release(sid: str, data: dict):
    """
    Release the presenter role.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call_id = data.get("call_id")
    if not call_id:
        return

    call = call_service.get_call(call_id)
    if not call:
        return

    result = presenter_service.release_presenter(call_id, user_id)

    if result.get("released"):
        # Log release with next presenter if any
        promoted = result.get("promoted")
        log_data = {
            "call_id": call_id,
            "user_id": user_id,
        }
        if promoted:
            log_data["next_presenter"] = promoted["user_id"]
        logger.info("presenter_release", **log_data)

        # Notify all participants that presenter was released
        for pid in call.participants:
            await emit_to_user("presenter_released", {
                "call_id": call_id,
                "user_id": user_id,
            }, pid)

        # If someone was auto-promoted from queue
        if promoted:
            for pid in call.participants:
                await emit_to_user("presenter_granted", {
                    "call_id": call_id,
                    "user_id": promoted["user_id"],
                    "display_name": promoted["display_name"],
                }, pid)

            # Notify the promoted user specifically
            await emit_to_user("presenter_promoted", {
                "call_id": call_id,
                "user_id": promoted["user_id"],
            }, promoted["user_id"])

        # Send queue update
        await _broadcast_queue_update(call_id, call)


@sio.event
async def presenter_cancel_request(sid: str, data: dict):
    """
    Cancel a queued presenter request.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call_id = data.get("call_id")
    if not call_id:
        return

    call = call_service.get_call(call_id)
    if not call:
        return

    presenter_service.cancel_request(call_id, user_id)
    await _broadcast_queue_update(call_id, call)


@sio.event
async def presenter_force_stop(sid: str, data: dict):
    """
    Admin force-stops the current presenter.
    data: { call_id: str, target_user_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    target_user_id = data.get("target_user_id")

    if not call_id or not target_user_id:
        return {"error": "call_id and target_user_id are required"}

    call = call_service.get_call(call_id)
    if not call:
        return {"error": "Call not found"}

    # Check if requester has admin rights (initiator is admin for now)
    if user_id != call.initiator_id:
        return {"error": "Only the call initiator can force-stop a presenter"}

    result = presenter_service.force_stop(call_id, target_user_id, user_id)

    if result.get("stopped"):
        # Notify all participants
        for pid in call.participants:
            await emit_to_user("presenter_force_stopped", {
                "call_id": call_id,
                "user_id": target_user_id,
                "stopped_by": user_id,
                "reason": "Stopped by call host",
            }, pid)

        # Also update the screen share state in call service
        call_service.toggle_screen_share(target_user_id, False)

    return result


@sio.event
async def presenter_get_state(sid: str, data: dict):
    """
    Get current presenter state for a call.
    data: { call_id: str }
    Returns: { current_presenter, queue, ... }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    if not call_id:
        return {"error": "call_id is required"}

    state = presenter_service.get_state(call_id)
    return state or {"current_presenter": None, "queue": []}


# ── Presenter Handoff ───────────────────────────────────────────

@sio.event
async def presenter_handoff(sid: str, data: dict):
    """
    Handoff presenter lock from current user to another participant.
    data: { call_id: str, to_user_id: str }

    Validates that the caller is the current presenter.
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return {"error": "Not authenticated"}

    call_id = data.get("call_id")
    to_user_id = data.get("to_user_id")

    if not call_id or not to_user_id:
        return {"error": "call_id and to_user_id are required"}

    call = call_service.get_call(call_id)
    if not call:
        return {"error": "Call not found"}

    # Verify caller is current presenter
    current_presenter = presenter_service.get_current_presenter(call_id)
    if current_presenter != user_id:
        return {
            "error": "Only the current presenter can initiate a handoff"
        }

    # Verify to_user is a call participant
    if to_user_id not in call.participants:
        return {"error": "Target user is not a participant in this call"}

    result = presenter_service.handoff_presenter(call_id, user_id, to_user_id)

    if result.get("status") == "handoff_accepted":
        # Log handoff from/to user
        logger.info(
            "presenter_handoff",
            call_id=call_id,
            from_user=user_id,
            to_user=to_user_id,
            handoff_count=result.get("handoff_count"),
        )

        # Notify old presenter that they've been released
        await emit_to_user("presenter_released", {
            "call_id": call_id,
            "user_id": user_id,
            "handoff": True,
        }, user_id)

        # Notify new presenter that they've been granted
        await emit_to_user("presenter_granted", {
            "call_id": call_id,
            "user_id": to_user_id,
            "via_handoff": True,
        }, to_user_id)

        # Notify all participants of handoff acceptance
        for pid in call.participants:
            await emit_to_user("presenter_handoff_accepted", {
                "call_id": call_id,
                "from_user": user_id,
                "to_user": to_user_id,
                "handoff_count": result.get("handoff_count"),
            }, pid)

        # Clear the presenter queue update
        await _broadcast_queue_update(call_id, call)

    return result


@sio.event
async def presenter_report_activity(sid: str, data: dict):
    """
    Report presenter activity (e.g., screen movement, keyboard input).
    This resets the inactivity timeout.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call_id = data.get("call_id")
    if not call_id:
        return

    # Verify user is the current presenter
    current_presenter = presenter_service.get_current_presenter(call_id)
    if current_presenter == user_id:
        presenter_service.report_activity(call_id, user_id)
        logger.debug(
            "presenter_activity_reset",
            call_id=call_id,
            user_id=user_id,
        )


@sio.event
async def presenter_viewer_join(sid: str, data: dict):
    """
    Register a user as a viewer of the screen share.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call_id = data.get("call_id")
    if not call_id:
        return

    call = call_service.get_call(call_id)
    if not call:
        return

    viewer_count = presenter_service.add_viewer(call_id, user_id)

    # Broadcast viewer count update to all participants
    for pid in call.participants:
        await emit_to_user("presenter_viewer_count", {
            "call_id": call_id,
            "viewer_count": viewer_count,
            "joined_user": user_id,
        }, pid)


@sio.event
async def presenter_viewer_leave(sid: str, data: dict):
    """
    Unregister a user as a viewer of the screen share.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call_id = data.get("call_id")
    if not call_id:
        return

    call = call_service.get_call(call_id)
    if not call:
        return

    viewer_count = presenter_service.remove_viewer(call_id, user_id)

    # Broadcast viewer count update to all participants
    for pid in call.participants:
        await emit_to_user("presenter_viewer_count", {
            "call_id": call_id,
            "viewer_count": viewer_count,
            "left_user": user_id,
        }, pid)


@sio.event
async def presenter_request_quality(sid: str, data: dict):
    """
    Viewer requests a quality change for the screen share.
    Relays the request to the current presenter.
    data: { call_id: str, quality: "low" | "medium" | "high" }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call_id = data.get("call_id")
    quality = data.get("quality", "medium")

    if not call_id:
        return

    # Get the current presenter
    presenter_user_id = presenter_service.get_current_presenter(call_id)
    if not presenter_user_id:
        return

    # Send quality request to the presenter's socket(s)
    await emit_to_user("presenter_quality_request", {
        "call_id": call_id,
        "requested_by": user_id,
        "quality": quality,
    }, presenter_user_id)

    logger.info(
        "presenter_quality_requested",
        call_id=call_id,
        requested_by=user_id,
        quality=quality,
        presenter=presenter_user_id,
    )


@sio.on("presenter_quality_request")
async def _presenter_quality_request_alias(sid: str, data: dict):
    """
    Client emits 'presenter_quality_request' (the same name the server uses
    when forwarding to the presenter). Delegate to presenter_request_quality.
    """
    return await presenter_request_quality(sid, data)


@sio.event
async def v2_screen_share_start(sid: str, data: dict):
    """
    Enhanced screen share start event with metadata.
    data: {
        call_id: str,
        source_type: "screen" | "window" | "tab",
        has_audio: bool,
        quality_preset: "low" | "medium" | "high"
    }
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

    # Verify user is the current presenter
    current_presenter = presenter_service.get_current_presenter(call_id)
    if current_presenter != user_id:
        return {"error": "Only the current presenter can start screen share"}

    source_type = data.get("source_type", "screen")
    has_audio = data.get("has_audio", False)
    quality_preset = data.get("quality_preset", "medium")

    # Broadcast screen share start to all participants
    for pid in call.participants:
        await emit_to_user("v2_screen:share_started", {
            "call_id": call_id,
            "presenter": user_id,
            "source_type": source_type,
            "has_audio": has_audio,
            "quality_preset": quality_preset,
            "started_at": time.time(),
        }, pid)

    logger.info(
        "v2_screen_share_started",
        call_id=call_id,
        user_id=user_id,
        source_type=source_type,
        has_audio=has_audio,
        quality_preset=quality_preset,
    )

    return {"status": "screen_share_started"}


@sio.event
async def v2_screen_share_stop(sid: str, data: dict):
    """
    Enhanced screen share stop event.
    data: { call_id: str }
    """
    user_id = await get_user_id(sid)
    if not user_id:
        return

    call_id = data.get("call_id")
    if not call_id:
        return

    call = call_service.get_call(call_id)
    if not call:
        return

    # Verify user is the current presenter
    current_presenter = presenter_service.get_current_presenter(call_id)
    if current_presenter != user_id:
        return

    # Broadcast screen share stop to all participants
    for pid in call.participants:
        await emit_to_user("v2_screen:share_stopped", {
            "call_id": call_id,
            "presenter": user_id,
            "stopped_at": time.time(),
        }, pid)

    logger.info(
        "v2_screen_share_stopped",
        call_id=call_id,
        user_id=user_id,
    )


@sio.event
async def presenter_get_metrics(sid: str, data: dict):
    """
    Get presenter metrics for a call.
    data: { call_id: str }

    Returns:
      {
        presenter_duration: float (seconds),
        queue_wait_times: [float, ...],
        handoff_count: int,
        viewer_count: int,
        current_presenter: str | None
      }
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

    metrics = presenter_service.get_presenter_metrics(call_id)
    return metrics


# ── Helper ───────────────────────────────────────────

async def _broadcast_queue_update(call_id: str, call) -> None:
    """Send the current presenter queue to all call participants."""
    queue = presenter_service.get_queue(call_id)
    for pid in call.participants:
        await emit_to_user("presenter_queue_update", {
            "call_id": call_id,
            "queue": queue,
        }, pid)
