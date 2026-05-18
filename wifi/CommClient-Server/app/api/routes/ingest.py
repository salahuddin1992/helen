"""
Ingest sources REST API — register / manage external camera feeds
(RTSP, RTMP, SRT, HTTP, HLS, NDI).

Endpoints
---------
Authenticated user:
  GET    /api/ingest/capabilities          — probe HW encoder + ffmpeg availability

Admin only:
  GET    /api/admin/ingest/sources         — list all sources + live status
  POST   /api/admin/ingest/sources         — register a new source
  GET    /api/admin/ingest/sources/{id}    — one source
  PATCH  /api/admin/ingest/sources/{id}    — edit
  DELETE /api/admin/ingest/sources/{id}    — unregister (stops if running)
  POST   /api/admin/ingest/sources/{id}/start    — start process
  POST   /api/admin/ingest/sources/{id}/stop     — stop process
  POST   /api/admin/ingest/sources/{id}/restart  — stop + start
  GET    /api/admin/ingest/sources/{id}/status   — live status + last log tail
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.gpu_detect import aprobe
from app.core.logging import get_logger
from app.core.security_utils import require_role
from app.models.media_policy import IngestSource
from app.services.ingest_service import ingest_service

logger = get_logger(__name__)


user_router = APIRouter(prefix="/ingest", tags=["ingest"])
admin_router = APIRouter(prefix="/admin/ingest", tags=["admin", "ingest"])


SUPPORTED_PROTOCOLS = {
    "rtsp", "rtmp", "srt", "http", "https", "hls",
    "file", "ndi",
    # Local capture: USB webcam / DSLR via capture card / UVC.
    # Resolved per-platform in ingest_service._build_ffmpeg_args.
    "usb",           # generic alias → dshow (Windows) / v4l2 (Linux) / avfoundation (Mac)
    "dshow",         # Windows DirectShow (explicit)
    "v4l2",          # Linux Video4Linux2 (explicit)
    "avfoundation",  # macOS AVFoundation (explicit)
    "mjpeg",         # MJPEG-over-HTTP (refresh-style IP cameras)
}
SUPPORTED_TRANSPORTS = {"tcp", "udp"}


# ── Pydantic ──────────────────────────────────────────────

class IngestCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    protocol: str = Field(..., description="rtsp|rtmp|srt|http|hls|file|ndi")
    url: str = Field(..., min_length=1, max_length=2048)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    transport: str = Field(default="tcp", description="tcp|udp (rtsp only)")
    codec_hint: str | None = Field(default=None, description="h264|hevc|mjpeg|av1")
    target_width: int | None = Field(default=None, ge=0, le=7680)
    target_height: int | None = Field(default=None, ge=0, le=4320)
    target_framerate: int | None = Field(default=None, ge=0, le=120)
    target_bitrate_kbps: int | None = Field(default=None, ge=0, le=200_000)
    enabled: bool = True
    auto_start: bool = False


class IngestUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    protocol: str | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    transport: str | None = None
    codec_hint: str | None = None
    target_width: int | None = Field(default=None, ge=0, le=7680)
    target_height: int | None = Field(default=None, ge=0, le=4320)
    target_framerate: int | None = Field(default=None, ge=0, le=120)
    target_bitrate_kbps: int | None = Field(default=None, ge=0, le=200_000)
    enabled: bool | None = None
    auto_start: bool | None = None


def _to_dict(src: IngestSource, include_password: bool = False) -> dict[str, Any]:
    live = ingest_service.get_status(src.id)
    out = {
        "id": src.id,
        "owner_user_id": src.owner_user_id,
        "name": src.name,
        "protocol": src.protocol,
        "url": src.url,
        "username": src.username,
        "transport": src.transport,
        "codec_hint": src.codec_hint,
        "target_width": src.target_width,
        "target_height": src.target_height,
        "target_framerate": src.target_framerate,
        "target_bitrate_kbps": src.target_bitrate_kbps,
        "enabled": src.enabled,
        "auto_start": src.auto_start,
        "status": live.get("status", src.status),
        "last_error": live.get("last_error") or src.last_error,
        "pid": live.get("pid"),
        "restart_count": live.get("restart_count"),
        "created_at": src.created_at.isoformat() if src.created_at else None,
        "updated_at": src.updated_at.isoformat() if src.updated_at else None,
    }
    if include_password:
        out["password"] = src.password
    return out


# ── User-facing ──────────────────────────────────────────

@user_router.get("/capabilities")
async def capabilities(
    user_id: str = Depends(require_role("user")),
):
    """
    Return available hardware encoders + ffmpeg path so the client can
    show the operator which ingest paths will work.
    """
    caps = await aprobe()
    return caps.as_dict()


# ── Admin ────────────────────────────────────────────────

@admin_router.get("/devices/local")
async def list_local_devices(user_id: str = Depends(require_role("admin"))):
    """Enumerate USB / capture-card video devices attached to the server."""
    from app.core.camera_discovery import list_local_cameras
    return {"devices": list_local_cameras()}


@admin_router.post("/devices/discover")
async def discover_network_cameras(
    user_id: str = Depends(require_role("admin")),
    timeout: float = 4.0,
):
    """Send an ONVIF WS-Discovery probe and return any responding NVTs."""
    from app.core.camera_discovery import discover_onvif
    found = await discover_onvif(timeout=max(1.0, min(10.0, timeout)))
    return {"cameras": found}


@admin_router.get("/sources")
async def list_sources(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(IngestSource))
    rows = list(result.scalars().all())
    return {"sources": [_to_dict(r) for r in rows]}


@admin_router.post("/sources", status_code=201)
async def create_source(
    payload: IngestCreate,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    proto = payload.protocol.lower()
    if proto not in SUPPORTED_PROTOCOLS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported protocol: {proto}",
        )
    if payload.transport.lower() not in SUPPORTED_TRANSPORTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported transport: {payload.transport}",
        )

    src = IngestSource(
        owner_user_id=user_id,
        name=payload.name,
        protocol=proto,
        url=payload.url.strip(),
        username=payload.username,
        password=payload.password,
        transport=payload.transport.lower(),
        codec_hint=payload.codec_hint,
        target_width=payload.target_width,
        target_height=payload.target_height,
        target_framerate=payload.target_framerate,
        target_bitrate_kbps=payload.target_bitrate_kbps,
        enabled=payload.enabled,
        auto_start=payload.auto_start,
        status="idle",
    )
    db.add(src)
    try:
        await db.commit()
        await db.refresh(src)
    except Exception as e:
        await db.rollback()
        logger.error("ingest_create_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register source",
        )

    audit_log("admin.ingest_create", user_id=user_id, success=True,
              details={"source_id": src.id, "protocol": proto})

    if payload.auto_start and payload.enabled:
        await ingest_service.start_source(src.id)

    return _to_dict(src)


@admin_router.get("/sources/{source_id}")
async def get_source(
    source_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(IngestSource).where(IngestSource.id == source_id))
    src = result.scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="source not found")
    return _to_dict(src)


@admin_router.patch("/sources/{source_id}")
async def update_source(
    source_id: str,
    payload: IngestUpdate,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(IngestSource).where(IngestSource.id == source_id))
    src = result.scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="source not found")

    data = payload.model_dump(exclude_none=True)
    if "protocol" in data and data["protocol"].lower() not in SUPPORTED_PROTOCOLS:
        raise HTTPException(400, detail=f"unsupported protocol: {data['protocol']}")
    if "transport" in data and data["transport"].lower() not in SUPPORTED_TRANSPORTS:
        raise HTTPException(400, detail=f"unsupported transport: {data['transport']}")

    for k, v in data.items():
        if k in {"protocol", "transport"} and isinstance(v, str):
            v = v.lower()
        setattr(src, k, v)
    try:
        await db.commit()
        await db.refresh(src)
    except Exception as e:
        await db.rollback()
        logger.error("ingest_update_failed", error=str(e))
        raise HTTPException(500, detail="Failed to update source")

    audit_log("admin.ingest_update", user_id=user_id, success=True,
              details={"source_id": source_id, "fields": list(data.keys())})

    # If it was running, restart so the new settings apply.
    if ingest_service.get_status(source_id).get("status") == "running":
        await ingest_service.restart_source(source_id)

    return _to_dict(src)


@admin_router.delete("/sources/{source_id}")
async def delete_source(
    source_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(IngestSource).where(IngestSource.id == source_id))
    src = result.scalar_one_or_none()
    if src is None:
        raise HTTPException(404, detail="source not found")
    await ingest_service.stop_source(source_id)
    await db.delete(src)
    await db.commit()
    audit_log("admin.ingest_delete", user_id=user_id, success=True,
              details={"source_id": source_id})
    return {"status": "deleted", "source_id": source_id}


@admin_router.post("/sources/{source_id}/start")
async def start_source(
    source_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(IngestSource).where(IngestSource.id == source_id))
    src = result.scalar_one_or_none()
    if src is None:
        raise HTTPException(404, detail="source not found")
    snap = await ingest_service.start_source(source_id)
    audit_log("admin.ingest_start", user_id=user_id, success=True,
              details={"source_id": source_id})
    return snap


@admin_router.post("/sources/{source_id}/stop")
async def stop_source(
    source_id: str,
    user_id: str = Depends(require_role("admin")),
):
    snap = await ingest_service.stop_source(source_id)
    audit_log("admin.ingest_stop", user_id=user_id, success=True,
              details={"source_id": source_id})
    return snap


@admin_router.post("/sources/{source_id}/restart")
async def restart_source(
    source_id: str,
    user_id: str = Depends(require_role("admin")),
):
    snap = await ingest_service.restart_source(source_id)
    audit_log("admin.ingest_restart", user_id=user_id, success=True,
              details={"source_id": source_id})
    return snap


@admin_router.get("/sources/{source_id}/status")
async def status_source(
    source_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(IngestSource).where(IngestSource.id == source_id))
    src = result.scalar_one_or_none()
    if src is None:
        raise HTTPException(404, detail="source not found")

    snap = ingest_service.get_status(source_id)
    log_tail: list[str] = []
    log_path = snap.get("log_path")
    if log_path:
        try:
            p = Path(log_path)
            if p.exists():
                # Last 40 lines, cheap on small logs.
                lines = p.read_bytes().splitlines()[-40:]
                log_tail = [ln.decode(errors="replace") for ln in lines]
        except Exception as e:
            logger.debug("ingest_log_tail_failed", error=str(e))

    return {
        "source": _to_dict(src),
        "runtime": snap,
        "log_tail": log_tail,
    }
