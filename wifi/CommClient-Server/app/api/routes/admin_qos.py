"""
Admin REST + WebSocket endpoints for the *Voice/Video QoS Live View* panel.

Mounted under ``/api/admin`` (prefix supplied by APIRouter). All HTTP
routes require role ``admin`` via ``require_role("admin")`` and emit
audit-log entries for every override.

Endpoints
---------
  GET  /calls/active                 — currently live calls.
  GET  /calls/{call_id}              — detailed call descriptor.
  GET  /calls/{call_id}/stats        — aggregated getStats() per stream.
  GET  /calls/{call_id}/rtcp         — last RTCP reports per participant.
  GET  /calls/{call_id}/jitter-buffer
  GET  /calls/{call_id}/bandwidth
  GET  /calls/{call_id}/codec-switches
  GET  /calls/{call_id}/quality-events
  POST /calls/{call_id}/force-preset
  POST /calls/{call_id}/force-codec
  POST /calls/{call_id}/toggle-simulcast
  POST /calls/{call_id}/force-turn
  POST /calls/{call_id}/chaos        — synthetic impairment injection.
  POST /calls/{call_id}/end          — terminate call.
  GET  /qos/summary                  — global aggregate snapshot.
  GET  /qos/topology/{call_id}       — mesh / SFU topology graph.
  GET  /qos/history                  — past calls search (call_logs).
  WS   /ws/qos                       — live 1-2Hz fan-out.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    APIRouter, Depends, HTTPException, Query, WebSocket,
    WebSocketDisconnect, status,
)
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.core.security import decode_token
from app.core.security_utils import require_role
from app.models.call_log import CallLog
from app.services.qos import (
    qos_admin_overrides,
    qos_anomaly_detector,
    qos_mesh_topology,
    qos_mos_calculator,
    qos_stats_collector,
    qos_ws_manager,
)

logger = get_logger(__name__)


router = APIRouter(prefix="/admin", tags=["admin-qos"])


# Convenience alias.
def require_admin():
    return require_role("admin")


# ── Pydantic request bodies ────────────────────────────────────────────

class ForcePresetBody(BaseModel):
    participant_id: str = Field(..., min_length=1)
    preset: str = Field(..., min_length=1)


class ForceCodecBody(BaseModel):
    participant_id: str = Field(..., min_length=1)
    codec_audio: str | None = None
    codec_video: str | None = None


class ToggleSimulcastBody(BaseModel):
    participant_id: str = Field(..., min_length=1)
    enabled: bool


class ForceTurnBody(BaseModel):
    participant_id: str = Field(..., min_length=1)


class ChaosBody(BaseModel):
    participant_id: str = Field(..., min_length=1)
    loss_pct: float | None = Field(None, ge=0, le=100)
    latency_ms: float | None = Field(None, ge=0, le=5000)


class EndCallBody(BaseModel):
    reason: str = Field("admin_terminated", max_length=64)


# ── Helpers ────────────────────────────────────────────────────────────

def _ensure_collector_hook() -> None:
    """Lazy-attach the QoS collector to the live ``call_service``."""
    try:
        qos_stats_collector.attach_to_call_service()
    except Exception:                                # pragma: no cover
        pass


def _get_call_or_404(call_id: str):
    from app.services.call_service import call_service
    call = call_service.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="call_not_found")
    return call


# ── /calls/active ──────────────────────────────────────────────────────

@router.get("/calls/active")
async def list_active_calls(user_id: str = Depends(require_role("admin"))):
    """All currently live calls with a one-line MOS summary."""
    _ensure_collector_hook()
    from app.services.call_service import call_service

    out: list[dict] = []
    now = datetime.now(timezone.utc)
    for cid, call in list(call_service._active_calls.items()):  # noqa: SLF001
        latest = qos_stats_collector.latest_per_participant(cid)
        mos_vals = [slot["mos_avg"] for slot in latest.values()]
        mos_avg = (sum(mos_vals) / len(mos_vals)) if mos_vals else 0.0
        out.append({
            "call_id": cid,
            "channel_id": call.channel_id,
            "initiator_id": call.initiator_id,
            "call_type": call.call_type,
            "routing": call.routing,
            "status": call.status,
            "participant_count": len(call.participants),
            "started_at": call.started_at.isoformat() if call.started_at else None,
            "duration_seconds": (
                int((now - call.started_at).total_seconds())
                if call.started_at else 0
            ),
            "mos_avg": round(mos_avg, 2),
            "quality_label": qos_mos_calculator.quality_label(mos_avg) if mos_avg else "n/a",
        })

    audit_log("admin.qos.list_active_calls", user_id=user_id, success=True,
              details={"count": len(out)})
    return {"calls": out, "count": len(out)}


# ── /calls/{call_id} ───────────────────────────────────────────────────

@router.get("/calls/{call_id}")
async def get_call_detail(call_id: str, user_id: str = Depends(require_role("admin"))):
    call = _get_call_or_404(call_id)
    latest = qos_stats_collector.latest_per_participant(call_id)
    participants = []
    for pid, pdata in call.participants.items():
        slot = latest.get(pid, {})
        participants.append({
            "user_id": pid,
            "joined_at": pdata["joined_at"].isoformat()
            if hasattr(pdata.get("joined_at"), "isoformat") else pdata.get("joined_at"),
            "muted": pdata.get("muted", False),
            "video_off": pdata.get("video_off", False),
            "sharing_screen": pdata.get("sharing_screen", False),
            "mos_avg": slot.get("mos_avg", 0.0),
            "stream_count": len(slot.get("streams") or {}),
        })

    return {
        "call_id": call.call_id,
        "channel_id": call.channel_id,
        "initiator_id": call.initiator_id,
        "call_type": call.call_type,
        "routing": call.routing,
        "status": call.status,
        "created_at": call.created_at.isoformat(),
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "participants": participants,
    }


# ── /calls/{call_id}/stats ─────────────────────────────────────────────

@router.get("/calls/{call_id}/stats")
async def get_call_stats(call_id: str, user_id: str = Depends(require_role("admin"))):
    _get_call_or_404(call_id)
    snap = qos_stats_collector.snapshot(call_id)
    return {
        "call_id": call_id,
        "streams": snap["streams"],
        "latest": snap["latest"],
    }


# ── /calls/{call_id}/rtcp ──────────────────────────────────────────────

@router.get("/calls/{call_id}/rtcp")
async def get_call_rtcp(call_id: str, user_id: str = Depends(require_role("admin"))):
    _get_call_or_404(call_id)
    snap = qos_stats_collector.snapshot(call_id)
    return {"call_id": call_id, "rtcp": snap["rtcp"]}


# ── /calls/{call_id}/jitter-buffer ─────────────────────────────────────

@router.get("/calls/{call_id}/jitter-buffer")
async def get_call_jitter_buffer(
    call_id: str, user_id: str = Depends(require_role("admin")),
):
    _get_call_or_404(call_id)
    snap = qos_stats_collector.snapshot(call_id)
    return {"call_id": call_id, "jitter_buffer": snap["jitter_buffer"]}


# ── /calls/{call_id}/bandwidth ─────────────────────────────────────────

@router.get("/calls/{call_id}/bandwidth")
async def get_call_bandwidth(
    call_id: str, user_id: str = Depends(require_role("admin")),
):
    _get_call_or_404(call_id)
    snap = qos_stats_collector.snapshot(call_id)
    return {"call_id": call_id, "bandwidth": snap["bandwidth"]}


# ── /calls/{call_id}/codec-switches ────────────────────────────────────

@router.get("/calls/{call_id}/codec-switches")
async def get_codec_switches(
    call_id: str, user_id: str = Depends(require_role("admin")),
):
    _get_call_or_404(call_id)
    snap = qos_stats_collector.snapshot(call_id)
    events = [e for e in snap["events"] if e.get("type") in ("codec_switch", "admin_force_codec")]
    return {"call_id": call_id, "events": events}


# ── /calls/{call_id}/quality-events ────────────────────────────────────

@router.get("/calls/{call_id}/quality-events")
async def get_quality_events(
    call_id: str, user_id: str = Depends(require_role("admin")),
):
    _get_call_or_404(call_id)
    snap = qos_stats_collector.snapshot(call_id)
    interesting = {"preset_switch", "codec_switch", "fec_event",
                   "simulcast_event", "quality_event",
                   "admin_force_preset", "admin_force_codec",
                   "admin_toggle_simulcast", "admin_force_turn",
                   "admin_chaos_inject"}
    events = [e for e in snap["events"] if e.get("type") in interesting]
    anomalies = [a.to_dict() for a in qos_anomaly_detector.detect(call_id)]
    return {"call_id": call_id, "events": events, "anomalies": anomalies}


# ── /calls/{call_id}/force-preset ──────────────────────────────────────

@router.post("/calls/{call_id}/force-preset", status_code=200)
async def force_preset(
    call_id: str, body: ForcePresetBody,
    user_id: str = Depends(require_role("admin")),
):
    _get_call_or_404(call_id)
    try:
        rec = await qos_admin_overrides.force_preset(
            call_id, body.participant_id, body.preset, actor_id=user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if rec.error == "call_not_found":
        raise HTTPException(status_code=404, detail=rec.error)
    if rec.error == "participant_not_in_call":
        raise HTTPException(status_code=404, detail=rec.error)
    return rec.to_dict()


# ── /calls/{call_id}/force-codec ───────────────────────────────────────

@router.post("/calls/{call_id}/force-codec", status_code=200)
async def force_codec(
    call_id: str, body: ForceCodecBody,
    user_id: str = Depends(require_role("admin")),
):
    _get_call_or_404(call_id)
    try:
        rec = await qos_admin_overrides.force_codec(
            call_id, body.participant_id, actor_id=user_id,
            codec_audio=body.codec_audio, codec_video=body.codec_video,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if rec.error in ("call_not_found", "participant_not_in_call"):
        raise HTTPException(status_code=404, detail=rec.error)
    return rec.to_dict()


# ── /calls/{call_id}/toggle-simulcast ──────────────────────────────────

@router.post("/calls/{call_id}/toggle-simulcast", status_code=200)
async def toggle_simulcast(
    call_id: str, body: ToggleSimulcastBody,
    user_id: str = Depends(require_role("admin")),
):
    _get_call_or_404(call_id)
    rec = await qos_admin_overrides.toggle_simulcast(
        call_id, body.participant_id, body.enabled, actor_id=user_id,
    )
    if rec.error in ("call_not_found", "participant_not_in_call"):
        raise HTTPException(status_code=404, detail=rec.error)
    return rec.to_dict()


# ── /calls/{call_id}/force-turn ────────────────────────────────────────

@router.post("/calls/{call_id}/force-turn", status_code=200)
async def force_turn(
    call_id: str, body: ForceTurnBody,
    user_id: str = Depends(require_role("admin")),
):
    _get_call_or_404(call_id)
    rec = await qos_admin_overrides.force_turn(
        call_id, body.participant_id, actor_id=user_id,
    )
    if rec.error in ("call_not_found", "participant_not_in_call"):
        raise HTTPException(status_code=404, detail=rec.error)
    return rec.to_dict()


# ── /calls/{call_id}/chaos ─────────────────────────────────────────────

@router.post("/calls/{call_id}/chaos", status_code=200)
async def chaos_inject(
    call_id: str, body: ChaosBody,
    user_id: str = Depends(require_role("admin")),
):
    _get_call_or_404(call_id)
    try:
        rec = await qos_admin_overrides.chaos_inject(
            call_id, body.participant_id, actor_id=user_id,
            loss_pct=body.loss_pct, latency_ms=body.latency_ms,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if rec.error in ("call_not_found", "participant_not_in_call"):
        raise HTTPException(status_code=404, detail=rec.error)
    return rec.to_dict()


# ── /calls/{call_id}/end ───────────────────────────────────────────────

@router.post("/calls/{call_id}/end", status_code=200)
async def force_end_call(
    call_id: str, body: EndCallBody,
    user_id: str = Depends(require_role("admin")),
):
    rec = await qos_admin_overrides.force_end(
        call_id, actor_id=user_id, reason=body.reason,
    )
    if rec.error == "call_not_found":
        raise HTTPException(status_code=404, detail=rec.error)
    if rec.error:
        raise HTTPException(status_code=500, detail=rec.error)
    return rec.to_dict()


# ── /qos/summary ───────────────────────────────────────────────────────

@router.get("/qos/summary")
async def qos_summary(user_id: str = Depends(require_role("admin"))):
    _ensure_collector_hook()
    summary = qos_stats_collector.aggregate_summary()
    anomalies = qos_anomaly_detector.detect_all()
    return {
        "summary": summary,
        "anomalies_by_call": {
            cid: [a.to_dict() for a in anns] for cid, anns in anomalies.items()
        },
        "anomalous_calls": list(anomalies.keys()),
        "generated_at": time.time(),
    }


# ── /qos/topology/{call_id} ────────────────────────────────────────────

@router.get("/qos/topology/{call_id}")
async def qos_topology(call_id: str, user_id: str = Depends(require_role("admin"))):
    _get_call_or_404(call_id)
    return qos_mesh_topology.build(call_id)


# ── /qos/history ───────────────────────────────────────────────────────

@router.get("/qos/history")
async def qos_history(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    participant: str | None = None,
    quality: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
):
    """
    Search past calls via the existing ``call_logs`` table.
    ``quality`` accepts excellent/good/fair/poor/bad and filters by
    metadata_json's stored MOS label (best-effort).
    """
    stmt = select(CallLog).order_by(CallLog.created_at.desc()).limit(limit)
    conds = []
    if from_:
        try:
            dt = datetime.fromisoformat(from_)
            conds.append(CallLog.created_at >= dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid 'from' timestamp")
    if to:
        try:
            dt = datetime.fromisoformat(to)
            conds.append(CallLog.created_at <= dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid 'to' timestamp")
    if participant:
        conds.append(CallLog.initiator_id == participant)
    if conds:
        stmt = stmt.where(and_(*conds))

    res = await db.execute(stmt)
    rows = res.scalars().all()
    items = []
    for r in rows:
        item = {
            "id": r.id,
            "channel_id": r.channel_id,
            "initiator_id": r.initiator_id,
            "call_type": r.call_type,
            "routing": r.routing,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "duration_seconds": r.duration_seconds,
            "end_reason": r.end_reason,
            "participant_count": r.participant_count,
        }
        if quality and r.metadata_json:
            try:
                import json as _j
                meta = _j.loads(r.metadata_json) or {}
                if meta.get("quality_label") != quality:
                    continue
                item["quality_label"] = meta.get("quality_label")
            except Exception:
                pass
        items.append(item)
    return {"items": items, "count": len(items)}


# ── WebSocket /ws/qos ──────────────────────────────────────────────────

async def _ws_authenticate(websocket: WebSocket) -> str:
    """
    Pull the Bearer token from either ``?token=`` query or the
    ``Sec-WebSocket-Protocol`` subprotocol header. Returns the user_id
    after verifying admin role.
    """
    token = websocket.query_params.get("token")
    if not token:
        # Browsers can only pass headers via the subprotocol slot.
        proto = websocket.headers.get("sec-websocket-protocol")
        if proto and proto.startswith("Bearer."):
            token = proto.split(".", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="missing token")
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="invalid token type")
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return payload.get("sub") or "anonymous"


@router.websocket("/ws/qos")
async def ws_qos(websocket: WebSocket):
    try:
        user_id = await _ws_authenticate(websocket)
    except HTTPException as e:
        await websocket.close(code=4401 if e.status_code == 401 else 4403)
        return

    _ensure_collector_hook()
    await websocket.accept()
    try:
        await websocket.send_json({"type": "hello", "ts": time.time(),
                                   "user_id": user_id})
        await qos_ws_manager.serve(websocket, user_id=user_id)
    except WebSocketDisconnect:
        pass
    except Exception as e:                           # pragma: no cover
        logger.warning("qos_ws_session_error", error=str(e))
