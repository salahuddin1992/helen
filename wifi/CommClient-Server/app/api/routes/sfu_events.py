"""
Inbound callback endpoint for the mediasoup SFU worker.

The Node worker POSTs JSON events to us (loopback only) when:
  - the AudioLevelObserver fires a new active speaker or silence
  - a recording is started / stopped (or ffmpeg exits)
  - a router is garbage-collected

We authenticate with a shared X-Sfu-Token header (set via the
`MEDIASOUP_EVENT_CALLBACK_TOKEN` env var that both the Node worker and
this Python server read).

Design constraints:
  - No DB write on the hot path — active-speaker events fire every 500ms
    per active call. We only fan out via Socket.IO.
  - Bound to the public /api/internal/sfu/events path; operators should
    firewall this to 127.0.0.1 at the reverse-proxy layer.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.core.logging import get_logger
from app.services.call_service import call_service
from app.services.presence_service import presence_service

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/sfu", tags=["internal"])


def _token() -> str | None:
    return os.environ.get("MEDIASOUP_EVENT_CALLBACK_TOKEN")


@router.post("/events", status_code=204)
async def sfu_events(
    request: Request,
    x_sfu_token: str | None = Header(default=None, alias="X-Sfu-Token"),
):
    expected = _token()
    if expected and x_sfu_token != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    evt = (payload or {}).get("type")
    call_id = (payload or {}).get("call_id")
    if not evt or not call_id:
        raise HTTPException(status_code=400, detail="missing type|call_id")

    call = call_service.get_call(call_id) if call_id else None

    # ── Active speaker ─────────────────────────────────────────────────────
    # High-frequency event: SFU's voice-activity detector fires
    # roughly 5 Hz on busy calls. Route through the broadcast
    # coalescer so we collapse 5 emits/sec down to 10/sec MAX
    # batched per-room — savings compound super-linearly with call
    # size and concurrent calls.
    if evt == "active_speaker":
        if not call:
            return
        out = {
            "call_id": call_id,
            "peer_id": payload.get("peer_id"),
            "producer_id": payload.get("producer_id"),
            "volume": payload.get("volume"),
            "ts": payload.get("ts"),
        }
        try:
            from app.services.broadcast_coalescer import get_broadcast_coalescer
            coalescer = get_broadcast_coalescer()
            if coalescer is not None:
                await coalescer.submit(
                    call_id=call_id,
                    event="call_sfu_active_speaker",
                    payload=out,
                )
                return
        except Exception:
            pass
        await _fanout(call, "call_sfu_active_speaker", out)
        return

    if evt == "silence":
        if not call:
            return
        try:
            from app.services.broadcast_coalescer import get_broadcast_coalescer
            coalescer = get_broadcast_coalescer()
            if coalescer is not None:
                await coalescer.submit(
                    call_id=call_id,
                    event="call_sfu_silence",
                    payload={"call_id": call_id, "ts": payload.get("ts")},
                )
                return
        except Exception:
            pass
        await _fanout(
            call,
            "call_sfu_silence",
            {"call_id": call_id, "ts": payload.get("ts")},
        )
        return

    # ── Recording lifecycle ────────────────────────────────────────────────
    if evt in ("recording_started", "recording_stopped"):
        if not call:
            return
        await _fanout(
            call,
            "call_sfu_recording_event",
            {
                "type": evt,
                "call_id": call_id,
                "recording_id": payload.get("recording_id"),
                "output_path": payload.get("output_path"),
                "exit_code": payload.get("exit_code"),
                "ts": payload.get("ts"),
            },
        )
        logger.info(
            "sfu_recording_event",
            type=evt,
            call_id=call_id,
            recording_id=payload.get("recording_id"),
            exit_code=payload.get("exit_code"),
        )
        return

    # Unknown events are tolerated (forward-compat) but logged at debug.
    logger.debug("sfu_event_unhandled", type=evt, call_id=call_id)


async def _fanout(call, event: str, payload: dict) -> None:
    """Emit ``event`` to every sid of every participant."""
    try:
        from app.socket.server import sio
    except Exception:
        return
    for uid in list(call.participants.keys()):
        try:
            for sid in presence_service.get_sids(uid) or []:
                try:
                    await sio.emit(event, payload, to=sid)
                except Exception:
                    pass
        except Exception:
            continue
