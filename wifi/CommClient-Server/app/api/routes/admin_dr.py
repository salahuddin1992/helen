"""
Phase 6 / Module AA — Disaster Recovery admin REST endpoints.

Mounted under ``/api/admin/dr``. Every endpoint requires the
``dr.manage`` RBAC permission. The endpoints are intentionally thin —
all logic lives in the ``app.services.dr`` package.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.dr import (
    VALID_BACKUP_KINDS,
    VALID_DESTINATION_KINDS,
    BackupDestination as DBDestination,
    BackupJob,
    DRDrill,
    RestoreOperation,
    RestorePoint,
)
from app.services.dr import drill_scheduler
from app.services.dr.backup_engine import backup_engine
from app.services.dr.destinations import build_destination, installed_destinations
from app.services.dr.restore_engine import restore_engine
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin/dr", tags=["admin-dr"])

_PERM = "dr.manage"


# ── shapes ──────────────────────────────────────────────────────


class BackupRequest(BaseModel):
    destination_id: Optional[str] = None
    encrypt: bool = True
    retention_days: Optional[int] = 30
    base_job_id: Optional[str] = None


class DestinationIn(BaseModel):
    name: str
    kind: str
    config: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class RestoreDryRunRequest(BaseModel):
    restore_point_id: str


class RestoreApplyRequest(BaseModel):
    restore_point_id: str
    confirmation_token: str
    operation_id: Optional[str] = None


class PointInTimeRequest(BaseModel):
    timestamp: datetime
    dry_run: bool = True


# ── jobs ────────────────────────────────────────────────────────


@router.get("/jobs")
async def list_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    kind: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = select(BackupJob)
    if kind:
        q = q.where(BackupJob.kind == kind)
    if status:
        q = q.where(BackupJob.status == status)
    q = q.order_by(desc(BackupJob.started_at)).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return {
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": j.id, "kind": j.kind, "status": j.status,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                "size_bytes": j.size_bytes, "sha256": j.sha256,
                "destination": j.destination, "encrypted": j.encrypted,
                "retention_until": j.retention_until.isoformat() if j.retention_until else None,
                "error": j.error_message,
            } for j in rows
        ],
    }


@router.post("/backup/full")
async def backup_full(
    body: BackupRequest = Body(default_factory=BackupRequest),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        res = await backup_engine.create_full(
            destination_id=body.destination_id,
            encrypt=body.encrypt,
            retention_days=body.retention_days,
        )
        audit_log("dr.backup_full", user_id=user_id, success=True,
                  details={"job_id": res.job_id})
        return {"job_id": res.job_id, "size_bytes": res.size_bytes,
                "sha256": res.sha256, "encrypted": res.encrypted,
                "duration_sec": res.duration_sec,
                "destination": res.destination}
    except Exception as e:
        audit_log("dr.backup_full", user_id=user_id, success=False,
                  details={"error": str(e)})
        raise HTTPException(500, detail=str(e))


@router.post("/backup/incremental")
async def backup_incremental(
    body: BackupRequest,
    user_id: str = Depends(require_permission(_PERM)),
):
    if not body.base_job_id:
        raise HTTPException(400, detail="base_job_id is required for incremental")
    try:
        res = await backup_engine.create_incremental(
            base_job_id=body.base_job_id,
            destination_id=body.destination_id,
            encrypt=body.encrypt,
            retention_days=body.retention_days,
        )
        audit_log("dr.backup_incremental", user_id=user_id, success=True,
                  details={"job_id": res.job_id})
        return {"job_id": res.job_id, "size_bytes": res.size_bytes,
                "sha256": res.sha256, "destination": res.destination}
    except Exception as e:
        audit_log("dr.backup_incremental", user_id=user_id, success=False,
                  details={"error": str(e)})
        raise HTTPException(500, detail=str(e))


@router.post("/backup/snapshot")
async def backup_snapshot(
    body: BackupRequest = Body(default_factory=BackupRequest),
    user_id: str = Depends(require_permission(_PERM)),
):
    res = await backup_engine.create_snapshot(
        destination_id=body.destination_id,
        encrypt=body.encrypt,
        retention_days=body.retention_days,
    )
    audit_log("dr.backup_snapshot", user_id=user_id, success=True,
              details={"job_id": res.job_id})
    return {"job_id": res.job_id, "size_bytes": res.size_bytes,
            "sha256": res.sha256, "destination": res.destination}


@router.post("/rotate")
async def rotate(
    keep_full: int = Query(7, ge=1, le=365),
    keep_incremental: int = Query(14, ge=1, le=365),
    user_id: str = Depends(require_permission(_PERM)),
):
    res = await backup_engine.rotate(
        keep_full=keep_full, keep_incremental=keep_incremental,
    )
    audit_log("dr.rotate", user_id=user_id, success=True, details=res)
    return res


# ── destinations ────────────────────────────────────────────────


@router.get("/destinations")
async def list_destinations(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(DBDestination).order_by(DBDestination.created_at.desc())
    )).scalars().all()
    return {
        "installed": installed_destinations(),
        "items": [
            {
                "id": d.id, "name": d.name, "kind": d.kind,
                "config": _mask_destination_config(d.kind, d.config or {}),
                "enabled": d.enabled,
                "last_used": d.last_used.isoformat() if d.last_used else None,
                "last_error": d.last_error,
            }
            for d in rows
        ],
    }


def _mask_destination_config(kind: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg or {})
    for key in ("secret_key", "password", "connection_string"):
        if key in out and out[key]:
            out[key] = "***"
    return out


@router.post("/destinations")
async def create_destination(
    body: DestinationIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.kind not in VALID_DESTINATION_KINDS:
        raise HTTPException(400, detail=f"invalid kind: {body.kind}")
    d = DBDestination(
        id=uuid.uuid4().hex, name=body.name, kind=body.kind,
        config=body.config, enabled=body.enabled,
    )
    db.add(d)
    await db.commit()
    audit_log("dr.destination_created", user_id=user_id, success=True,
              details={"id": d.id, "kind": d.kind})
    return {"id": d.id, "name": d.name, "kind": d.kind, "enabled": d.enabled}


@router.delete("/destinations/{destination_id}")
async def delete_destination(
    destination_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(DBDestination).where(DBDestination.id == destination_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="destination not found")
    await db.delete(row)
    await db.commit()
    audit_log("dr.destination_deleted", user_id=user_id, success=True,
              details={"id": destination_id})
    return {"ok": True}


@router.post("/destinations/{destination_id}/test")
async def test_destination(
    destination_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(DBDestination).where(DBDestination.id == destination_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="destination not found")
    try:
        dest = build_destination(row.kind, row.config or {})
        h = await dest.health()
        if not h.get("ok"):
            row.last_error = str(h.get("error") or "unknown")
        else:
            row.last_error = None
            row.last_used = datetime.now(timezone.utc)
        await db.commit()
        audit_log("dr.destination_tested", user_id=user_id,
                  success=bool(h.get("ok")), details={"id": destination_id})
        return h
    except Exception as e:
        row.last_error = str(e)
        await db.commit()
        return {"ok": False, "error": str(e)}


# ── restore ─────────────────────────────────────────────────────


@router.get("/restore-points")
async def list_restore_points(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(RestorePoint).order_by(desc(RestorePoint.created_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "items": [
            {
                "id": r.id, "backup_job_id": r.backup_job_id,
                "schema_version": r.schema_version, "app_version": r.app_version,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r in rows
        ],
    }


@router.post("/restore/dry-run")
async def restore_dry_run(
    body: RestoreDryRunRequest,
    user_id: str = Depends(require_permission(_PERM)),
):
    res = await restore_engine.restore_full(
        body.restore_point_id, initiated_by=user_id, dry_run=True,
    )
    audit_log("dr.restore_dry_run", user_id=user_id, success=True,
              details={"restore_point_id": body.restore_point_id,
                       "operation_id": res.get("operation_id")})
    return res


@router.post("/restore/apply")
async def restore_apply(
    body: RestoreApplyRequest,
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        res = await restore_engine.restore_full(
            body.restore_point_id, initiated_by=user_id,
            dry_run=False, confirmation_token=body.confirmation_token,
        )
        audit_log("dr.restore_apply", user_id=user_id, success=True,
                  details={"operation_id": res.get("operation_id")})
        return res
    except PermissionError as e:
        raise HTTPException(403, detail=str(e))


@router.post("/restore/point-in-time")
async def restore_point_in_time(
    body: PointInTimeRequest,
    user_id: str = Depends(require_permission(_PERM)),
):
    res = await restore_engine.restore_to_point_in_time(
        body.timestamp, initiated_by=user_id, dry_run=body.dry_run,
    )
    return res


@router.get("/restore-operations")
async def list_restore_operations(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(RestoreOperation).order_by(desc(RestoreOperation.started_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "items": [
            {
                "id": r.id, "restore_point_id": r.restore_point_id,
                "initiated_by": r.initiated_by, "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "dry_run": r.dry_run,
                "error": r.error_message,
            } for r in rows
        ],
    }


# ── drills ──────────────────────────────────────────────────────


@router.get("/drills")
async def list_drills(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(DRDrill).order_by(desc(DRDrill.executed_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "items": [
            {
                "id": d.id,
                "scheduled_at": d.scheduled_at.isoformat() if d.scheduled_at else None,
                "executed_at": d.executed_at.isoformat() if d.executed_at else None,
                "success": d.success,
                "rto_seconds": d.rto_seconds, "rpo_seconds": d.rpo_seconds,
                "report": d.report,
            } for d in rows
        ],
    }


@router.post("/drills/run-now")
async def drill_run_now(
    user_id: str = Depends(require_permission(_PERM)),
):
    rep = await drill_scheduler.run_once()
    audit_log("dr.drill_run_now", user_id=user_id, success=bool(rep.get("ok")),
              details={"rto": rep.get("rto_seconds"),
                       "rpo": rep.get("rpo_seconds")})
    return rep


@router.get("/rto-rpo")
async def rto_rpo(
    _user: str = Depends(require_permission(_PERM)),
):
    summary = await drill_scheduler.rto_rpo_summary()
    summary["scheduler_enabled"] = drill_scheduler.get_state().enabled
    summary["interval_hours"] = drill_scheduler.get_state().interval_hours
    return summary
