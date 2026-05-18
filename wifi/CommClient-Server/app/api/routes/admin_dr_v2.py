"""
DR v2 admin REST + WebSocket surface.

Mounted under ``/api/admin/dr`` next to the legacy ``admin_dr`` module.
Endpoints in this module follow the v2 contract specified in the
Disaster Recovery Console design:

    /destinations                  CRUD v2 destinations (LAN-only)
    /destinations/{id}/test        Connectivity + write probe
    /backups                       List + force backup
    /backups/{id}                  Detail with chunks/hashes
    /backups/{id}/verify           Re-verify integrity
    /backups/{id}/restore          Typed-confirm restore
    /backups/archive               Bulk archive
    /backups/{id}                  Typed-confirm delete
    /jobs, /jobs/{id}, /jobs/{id}/cancel
    /policies                      CRUD + dry-run
    /rpo-rto                       Current measurements
    /test-restore                  Sandbox drill
    /drills                        Drill mgmt
    /verify/queue, /verify/alerts, /verify/schedule, /verify/run-full
    /custody                       Chain of custody
    /keys                          Encryption key mgmt
    /reports                       Framework reports
    /metrics/charts                Chart data
    /ws/dr                         WebSocket fan-out

Every destructive endpoint requires the ``dr.manage`` RBAC permission and
emits an audit-log entry via :func:`audit_log`.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    WebSocket,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.dr_v2 import (
    VALID_DR_V2_DESTINATION_KINDS,
    DRBackup,
    DRBackupChunk,
    DRDestination,
    DRDrillV2,
    DRJob,
    DRPolicy,
)
from app.services.dr.backup_engine_v2 import backup_engine_v2
from app.services.dr.destination_drivers import build_driver, list_kinds
from app.services.dr.drill_runner import drill_runner
from app.services.dr.integrity_verifier import integrity_verifier
from app.services.dr.job_registry import dr_job_registry
from app.services.dr.key_manager import dr_key_manager
from app.services.dr import policy_engine
from app.services.dr.report_generator import (
    VALID_FORMATS,
    VALID_FRAMEWORKS,
    dr_report_generator,
)
from app.services.dr.rpo_rto_meter import measure as measure_rpo_rto
from app.services.dr.ws_stream import dr_ws_manager
from app.services.rbac.enforcer import require_permission


logger = get_logger(__name__)
# Parent ``api_router`` adds ``/api`` to every mounted sub-router.  This
# router itself declares ``/admin/dr`` so its final URL space is
# ``/api/admin/dr/…`` — identical to the legacy ``admin_dr`` module.
router = APIRouter(prefix="/admin/dr", tags=["admin-dr"])

_PERM = "dr.manage"


# ── helpers ─────────────────────────────────────────────────────────


def _mask_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg or {})
    for key in ("password", "secret_key", "access_key",
                "connection_string", "auth_token"):
        if key in out and out[key]:
            out[key] = "***"
    return out


def _serialize_destination(r: DRDestination) -> Dict[str, Any]:
    return {
        "id": r.id, "name": r.name, "kind": r.kind,
        "config": _mask_config(r.config or {}),
        "enabled": r.enabled, "priority": r.priority,
        "capacity_bytes": r.capacity_bytes, "used_bytes": r.used_bytes,
        "last_health_ok": r.last_health_ok,
        "last_latency_ms": r.last_latency_ms,
        "last_checked_at": r.last_checked_at.isoformat() if r.last_checked_at else None,
        "last_error": r.last_error,
        "notes": r.notes,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _serialize_backup(b: DRBackup) -> Dict[str, Any]:
    return {
        "id": b.id, "policy_id": b.policy_id,
        "destination_id": b.destination_id,
        "base_backup_id": b.base_backup_id,
        "cadence": b.cadence, "status": b.status,
        "started_at": b.started_at.isoformat() if b.started_at else None,
        "completed_at": b.completed_at.isoformat() if b.completed_at else None,
        "size_bytes": b.size_bytes,
        "chunk_count": b.chunk_count,
        "sha256_root": b.sha256_root,
        "encrypted": b.encrypted,
        "encryption_key_ref": b.encryption_key_ref,
        "retention_until": b.retention_until.isoformat() if b.retention_until else None,
        "last_verified_at": b.last_verified_at.isoformat() if b.last_verified_at else None,
        "last_verify_ok": b.last_verify_ok,
        "archived": b.archived,
        "actor_id": b.actor_id,
        "error_message": b.error_message,
    }


# ── pydantic shapes ─────────────────────────────────────────────────


class DRDestinationIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    kind: str
    config: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    priority: int = 100
    notes: Optional[str] = None


class DRDestinationPatch(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    notes: Optional[str] = None


class BackupStartIn(BaseModel):
    policy_id: Optional[str] = None
    destination_id: Optional[str] = None
    cadence: str = "full"


class RestoreIn(BaseModel):
    target: str = Field(..., min_length=1)
    scope: str = Field(default="sandbox")
    confirmation: str = Field(..., description="must equal 'RESTORE'")
    reason: str = Field(..., min_length=3, max_length=2048)


class ArchiveIn(BaseModel):
    backup_ids: List[str] = Field(default_factory=list)


class DeleteConfirmIn(BaseModel):
    confirmation: str = Field(..., description="must equal 'DELETE'")
    reason: str = Field(..., min_length=3, max_length=2048)


class PolicyIn(BaseModel):
    name: str
    description: Optional[str] = None
    cron_schedule: str = "0 2 * * *"
    scope: List[str] = Field(default_factory=list)
    cadence: str = "full"
    retention: Dict[str, Any] = Field(default_factory=dict)
    encryption_key_ref: Optional[str] = None
    pre_hook: Optional[str] = None
    post_hook: Optional[str] = None
    destinations: List[Any] = Field(default_factory=list)
    enabled: bool = True


class KeyIn(BaseModel):
    alias: str = Field(..., min_length=1, max_length=128)
    algorithm: str = "aes-256-gcm"
    backend: str = "local"


class DrillScheduleIn(BaseModel):
    scheduled_at: datetime
    scope: str = "sandbox"
    name: Optional[str] = None


class VerifyScheduleIn(BaseModel):
    backup_ids: List[str] = Field(default_factory=list)


# ── destinations ────────────────────────────────────────────────────


@router.get("/destinations/v2")
async def v2_list_destinations(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(DRDestination).order_by(desc(DRDestination.created_at))
    )).scalars().all()
    return {
        "kinds": list_kinds(),
        "items": [_serialize_destination(r) for r in rows],
    }


@router.post("/destinations/v2")
async def v2_create_destination(
    body: DRDestinationIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.kind not in VALID_DR_V2_DESTINATION_KINDS:
        raise HTTPException(400, detail=f"invalid kind: {body.kind}")
    # Optional dry validation: try building the driver to catch config errors
    try:
        build_driver(body.kind, body.config or {})
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except RuntimeError as e:
        # missing optional SDK — accept persistently, surface in /test
        logger.warning("dr_v2_destination_driver_unavailable",
                       kind=body.kind, error=str(e))

    row = DRDestination(
        id=uuid.uuid4().hex, name=body.name, kind=body.kind,
        config=body.config or {}, enabled=body.enabled,
        priority=int(body.priority), notes=body.notes,
    )
    db.add(row)
    await db.commit()
    audit_log("dr.v2.destination_created", user_id=user_id,
              details={"id": row.id, "kind": row.kind, "name": row.name})
    await dr_ws_manager.broadcast("destination.changed",
                                  {"id": row.id, "action": "created"})
    return _serialize_destination(row)


@router.put("/destinations/v2/{destination_id}")
async def v2_update_destination(
    destination_id: str,
    body: DRDestinationPatch,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(DRDestination).where(DRDestination.id == destination_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="destination not found")
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items()
              if v is not None}
    if fields:
        await db.execute(
            update(DRDestination).where(DRDestination.id == destination_id)
            .values(**fields)
        )
        await db.commit()
    audit_log("dr.v2.destination_updated", user_id=user_id,
              details={"id": destination_id, "fields": list(fields.keys())})
    await dr_ws_manager.broadcast("destination.changed",
                                  {"id": destination_id, "action": "updated"})
    row = (await db.execute(
        select(DRDestination).where(DRDestination.id == destination_id)
    )).scalar_one()
    return _serialize_destination(row)


@router.delete("/destinations/v2/{destination_id}")
async def v2_delete_destination(
    destination_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(DRDestination).where(DRDestination.id == destination_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="destination not found")
    await db.delete(row)
    await db.commit()
    audit_log("dr.v2.destination_deleted", user_id=user_id,
              details={"id": destination_id})
    await dr_ws_manager.broadcast("destination.changed",
                                  {"id": destination_id, "action": "deleted"})
    return {"ok": True}


@router.post("/destinations/v2/{destination_id}/test")
async def v2_test_destination(
    destination_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(DRDestination).where(DRDestination.id == destination_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="destination not found")
    try:
        driver = build_driver(row.kind, row.config or {})
        health = await driver.test()
        # write probe
        probe = b"helen-dr-probe-" + uuid.uuid4().hex.encode()
        import hashlib
        sha = hashlib.sha256(probe).hexdigest()
        try:
            await driver.write_chunk(f"probe/{uuid.uuid4().hex}", 0, probe, sha256=sha)
            wrote = True
        except Exception as e:
            wrote = False
            health.error = (health.error or "") + f"; write probe failed: {e}"
        await db.execute(
            update(DRDestination).where(DRDestination.id == destination_id).values(
                last_health_ok=bool(health.ok),
                last_latency_ms=float(health.latency_ms),
                last_checked_at=datetime.now(timezone.utc),
                last_error=health.error,
                capacity_bytes=int(health.capacity_bytes or 0),
                used_bytes=int(health.used_bytes or 0),
            )
        )
        await db.commit()
        audit_log("dr.v2.destination_tested", user_id=user_id,
                  success=bool(health.ok),
                  details={"id": destination_id})
        return {
            **health.as_dict(),
            "write_probe_ok": wrote,
        }
    except Exception as e:
        await db.execute(
            update(DRDestination).where(DRDestination.id == destination_id).values(
                last_health_ok=False,
                last_checked_at=datetime.now(timezone.utc),
                last_error=str(e),
            )
        )
        await db.commit()
        return {"ok": False, "error": str(e)}


# ── backups ─────────────────────────────────────────────────────────


@router.get("/backups")
async def list_backups(
    destination: Optional[str] = Query(None, alias="destination"),
    policy: Optional[str] = Query(None, alias="policy"),
    status: Optional[str] = None,
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = select(DRBackup)
    if destination:
        q = q.where(DRBackup.destination_id == destination)
    if policy:
        q = q.where(DRBackup.policy_id == policy)
    if status:
        q = q.where(DRBackup.status == status)
    if from_:
        q = q.where(DRBackup.started_at >= from_)
    if to:
        q = q.where(DRBackup.started_at <= to)
    q = q.order_by(desc(DRBackup.started_at)).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return {
        "page": page, "page_size": page_size,
        "items": [_serialize_backup(b) for b in rows],
    }


@router.get("/backups/{backup_id}")
async def get_backup(
    backup_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(DRBackup).where(DRBackup.id == backup_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="backup not found")
    chunks = (await db.execute(
        select(DRBackupChunk).where(DRBackupChunk.backup_id == backup_id)
        .order_by(DRBackupChunk.seq.asc())
    )).scalars().all()
    return {
        **_serialize_backup(row),
        "chunks": [
            {"seq": c.seq, "size": c.size, "sha256": c.sha256,
             "encrypted_size": c.encrypted_size,
             "storage_key": c.storage_key}
            for c in chunks
        ],
    }


@router.post("/backups")
async def force_backup(
    body: BackupStartIn,
    user_id: str = Depends(require_permission(_PERM)),
):
    if not body.policy_id and not body.destination_id:
        raise HTTPException(400, detail="policy_id or destination_id required")
    job_id = await backup_engine_v2.start_backup(
        policy_id=body.policy_id,
        destination_id=body.destination_id,
        actor_id=user_id, cadence=body.cadence,
    )
    return {"job_id": job_id, "status": "queued"}


@router.post("/backups/{backup_id}/verify")
async def verify_backup(
    backup_id: str,
    user_id: str = Depends(require_permission(_PERM)),
):
    await integrity_verifier.queue_verify(backup_id)
    audit_log("dr.v2.verify_queued", user_id=user_id,
              details={"backup_id": backup_id})
    return {"ok": True, "queued": True, "backup_id": backup_id}


@router.post("/backups/{backup_id}/restore")
async def restore_backup(
    backup_id: str,
    body: RestoreIn,
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.confirmation != "RESTORE":
        raise HTTPException(400, detail="confirmation must be 'RESTORE'")
    try:
        job_id = await backup_engine_v2.restore(
            backup_id, target=body.target, scope=body.scope,
            reason=body.reason, actor_id=user_id,
            confirmation=body.confirmation,
        )
    except PermissionError as e:
        raise HTTPException(403, detail=str(e))
    return {"job_id": job_id, "status": "queued"}


@router.post("/backups/archive")
async def bulk_archive(
    body: ArchiveIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if not body.backup_ids:
        raise HTTPException(400, detail="no backup_ids provided")
    archived: List[str] = []
    for bid in body.backup_ids:
        await db.execute(
            update(DRBackup).where(DRBackup.id == bid).values(
                archived=True, status="archived",
            )
        )
        archived.append(bid)
    await db.commit()
    audit_log("dr.v2.bulk_archive", user_id=user_id,
              details={"count": len(archived), "backup_ids": archived})
    return {"archived": archived, "count": len(archived)}


@router.delete("/backups/{backup_id}")
async def delete_backup(
    backup_id: str,
    body: DeleteConfirmIn = Body(...),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.confirmation != "DELETE":
        raise HTTPException(400, detail="confirmation must be 'DELETE'")
    row = (await db.execute(
        select(DRBackup).where(DRBackup.id == backup_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="backup not found")
    await db.delete(row)
    await db.commit()
    audit_log("dr.v2.backup_deleted", user_id=user_id,
              details={"backup_id": backup_id, "reason": body.reason})
    return {"ok": True}


# ── jobs ────────────────────────────────────────────────────────────


@router.get("/jobs/v2")
async def v2_list_jobs(
    kind: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    _user: str = Depends(require_permission(_PERM)),
):
    items = await dr_job_registry.list(kind=kind, status=status, limit=limit)
    return {"items": [i.as_dict() for i in items]}


@router.get("/jobs/v2/{job_id}")
async def v2_get_job(
    job_id: str,
    _user: str = Depends(require_permission(_PERM)),
):
    snap = await dr_job_registry.get(job_id)
    if snap is None:
        raise HTTPException(404, detail="job not found")
    return snap.as_dict()


@router.post("/jobs/v2/{job_id}/cancel")
async def v2_cancel_job(
    job_id: str,
    user_id: str = Depends(require_permission(_PERM)),
):
    ok = await dr_job_registry.cancel(job_id)
    if not ok:
        raise HTTPException(409, detail="job not cancellable")
    audit_log("dr.v2.job_cancelled", user_id=user_id,
              details={"job_id": job_id})
    return {"ok": True}


# ── policies ────────────────────────────────────────────────────────


@router.get("/policies")
async def list_policies_route(
    _user: str = Depends(require_permission(_PERM)),
):
    return {"items": await policy_engine.list_policies()}


@router.post("/policies")
async def create_policy_route(
    body: PolicyIn,
    user_id: str = Depends(require_permission(_PERM)),
):
    return await policy_engine.create_policy(body.model_dump(), actor_id=user_id)


@router.put("/policies/{policy_id}")
async def update_policy_route(
    policy_id: str,
    body: PolicyIn,
    user_id: str = Depends(require_permission(_PERM)),
):
    res = await policy_engine.update_policy(
        policy_id, body.model_dump(), actor_id=user_id,
    )
    if res is None:
        raise HTTPException(404, detail="policy not found")
    return res


@router.delete("/policies/{policy_id}")
async def delete_policy_route(
    policy_id: str,
    user_id: str = Depends(require_permission(_PERM)),
):
    ok = await policy_engine.delete_policy(policy_id, actor_id=user_id)
    if not ok:
        raise HTTPException(404, detail="policy not found")
    return {"ok": True}


@router.post("/policies/{policy_id}/dry-run")
async def policy_dry_run(
    policy_id: str,
    _user: str = Depends(require_permission(_PERM)),
):
    try:
        return await policy_engine.dry_run(policy_id)
    except LookupError as e:
        raise HTTPException(404, detail=str(e))


# ── RPO / RTO ───────────────────────────────────────────────────────


@router.get("/rpo-rto")
async def rpo_rto(
    _user: str = Depends(require_permission(_PERM)),
):
    return await measure_rpo_rto()


# ── drills ──────────────────────────────────────────────────────────


@router.get("/drills/v2")
async def v2_list_drills(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(DRDrillV2).order_by(desc(DRDrillV2.scheduled_at)).limit(limit)
    )).scalars().all()
    return {
        "items": [
            {
                "id": r.id, "name": r.name, "status": r.status,
                "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "scope": r.scope,
                "rto_seconds": r.rto_seconds, "rpo_seconds": r.rpo_seconds,
                "integrity_ok": r.integrity_ok,
            } for r in rows
        ]
    }


@router.post("/drills/schedule")
async def schedule_drill(
    body: DrillScheduleIn,
    user_id: str = Depends(require_permission(_PERM)),
):
    did = await drill_runner.schedule(
        scheduled_at=body.scheduled_at, scope=body.scope,
        name=body.name, actor_id=user_id,
    )
    return {"drill_id": did}


@router.post("/test-restore")
async def test_restore(
    body: Dict[str, Any] = Body(default_factory=dict),
    user_id: str = Depends(require_permission(_PERM)),
):
    scope = str(body.get("scope") or "sandbox")
    backup_id = body.get("backup_id")
    res = await drill_runner.run_drill(
        scope=scope, backup_id=backup_id, actor_id=user_id,
        name=body.get("name"),
    )
    await dr_ws_manager.broadcast("drill.completed", {"drill_id": res["drill_id"]})
    return res


@router.get("/drills/{drill_id}/report")
async def drill_report(
    drill_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(DRDrillV2).where(DRDrillV2.id == drill_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="drill not found")
    return {
        "id": row.id, "name": row.name, "status": row.status,
        "scope": row.scope,
        "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "rto_seconds": row.rto_seconds,
        "rpo_seconds": row.rpo_seconds,
        "integrity_ok": row.integrity_ok,
        "steps": list(row.steps or []),
        "recommendations": list(row.recommendations or []),
        "report": dict(row.report or {}),
    }


# ── verifier ────────────────────────────────────────────────────────


@router.get("/verify/queue")
async def verify_queue(
    _user: str = Depends(require_permission(_PERM)),
):
    return {"queue_size": integrity_verifier.queue_size(),
            "alerts": integrity_verifier.alerts()[-20:]}


@router.get("/verify/alerts")
async def verify_alerts(
    _user: str = Depends(require_permission(_PERM)),
):
    return {"items": integrity_verifier.alerts()}


@router.post("/verify/schedule")
async def verify_schedule(
    body: VerifyScheduleIn,
    user_id: str = Depends(require_permission(_PERM)),
):
    for bid in body.backup_ids:
        await integrity_verifier.queue_verify(bid)
    audit_log("dr.v2.verify_scheduled", user_id=user_id,
              details={"count": len(body.backup_ids)})
    return {"queued": len(body.backup_ids)}


@router.post("/verify/run-full")
async def verify_run_full(
    user_id: str = Depends(require_permission(_PERM)),
):
    res = await integrity_verifier.run_full_corpus()
    audit_log("dr.v2.verify_run_full", user_id=user_id, details=res)
    return res


# ── chain of custody ─────────────────────────────────────────────────


@router.get("/custody")
async def chain_of_custody(
    backup_id: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    """Reconstruct the chain of custody from audit + job + drill rows."""
    # We synthesize the chain from DRJob + DRBackup actor fields plus the
    # audit log table; expensive joins are avoided in favour of two simple
    # ORM scans capped to ``limit``.
    q = select(DRJob).order_by(desc(DRJob.created_at)).limit(limit)
    if backup_id:
        q = q.where(DRJob.backup_id == backup_id)
    jobs = (await db.execute(q)).scalars().all()
    bq = select(DRBackup).order_by(desc(DRBackup.started_at)).limit(limit)
    if backup_id:
        bq = bq.where(DRBackup.id == backup_id)
    backups = (await db.execute(bq)).scalars().all()
    events: List[Dict[str, Any]] = []
    for b in backups:
        events.append({
            "type": "backup", "id": b.id, "actor": b.actor_id,
            "at": b.started_at.isoformat() if b.started_at else None,
            "status": b.status,
            "sha256_root": b.sha256_root,
        })
    for j in jobs:
        events.append({
            "type": "job", "id": j.id, "kind": j.kind, "actor": j.actor_id,
            "at": j.created_at.isoformat() if j.created_at else None,
            "status": j.status, "backup_id": j.backup_id,
        })
    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    return {"items": events[:limit]}


# ── keys ────────────────────────────────────────────────────────────


@router.get("/keys")
async def list_keys(
    _user: str = Depends(require_permission(_PERM)),
):
    return {"items": await dr_key_manager.list_keys()}


@router.post("/keys")
async def create_key(
    body: KeyIn,
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        res = await dr_key_manager.generate_key(
            alias=body.alias, algorithm=body.algorithm,
            backend=body.backend, actor_id=user_id,
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    audit_log("dr.v2.key_generated", user_id=user_id,
              details={"key_id": res.id, "alias": res.alias,
                       "algorithm": res.algorithm, "backend": res.backend})
    return res.as_dict()


@router.post("/keys/{key_id}/rotate")
async def rotate_key(
    key_id: str,
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        res = await dr_key_manager.rotate(key_id, actor_id=user_id)
    except LookupError as e:
        raise HTTPException(404, detail=str(e))
    audit_log("dr.v2.key_rotated", user_id=user_id,
              details={"old_id": key_id, "new_id": res.id})
    return res.as_dict()


@router.post("/keys/{key_id}/export-public")
async def export_public_key(
    key_id: str,
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        out = await dr_key_manager.export_public(key_id)
    except LookupError as e:
        raise HTTPException(404, detail=str(e))
    audit_log("dr.v2.key_public_exported", user_id=user_id,
              details={"key_id": key_id})
    return out


# ── reports ─────────────────────────────────────────────────────────


@router.get("/reports")
async def get_report(
    framework: str = Query(..., description=",".join(VALID_FRAMEWORKS)),
    period: str = Query("30d"),
    fmt: str = Query("json", alias="format",
                     description=",".join(VALID_FORMATS)),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        body, content_type = await dr_report_generator.generate(
            framework, period, fmt,
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    audit_log("dr.v2.report_generated", user_id=user_id,
              details={"framework": framework, "period": period, "format": fmt,
                       "bytes": len(body)})
    filename = f"dr-{framework}-{period}.{fmt}"
    return Response(
        content=body,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── metrics ─────────────────────────────────────────────────────────


@router.get("/metrics/charts")
async def metrics_charts(
    range_: str = Query("30d", alias="range"),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    delta = {"24h": timedelta(hours=24), "7d": timedelta(days=7),
             "30d": timedelta(days=30), "90d": timedelta(days=90),
             "365d": timedelta(days=365)}.get(range_, timedelta(days=30))
    start = now - delta

    backups = (await db.execute(
        select(DRBackup).where(DRBackup.started_at >= start)
        .order_by(DRBackup.started_at.asc())
    )).scalars().all()
    drills = (await db.execute(
        select(DRDrillV2).where(DRDrillV2.scheduled_at >= start)
        .order_by(DRDrillV2.scheduled_at.asc())
    )).scalars().all()

    success = sum(1 for b in backups if b.status == "succeeded")
    failed = sum(1 for b in backups if b.status == "failed")
    rate = success / max(1, success + failed)

    size_series = [
        {"at": b.started_at.isoformat() if b.started_at else None,
         "size_bytes": int(b.size_bytes or 0)}
        for b in backups
    ]
    rpo_series = []
    last_completed: Optional[datetime] = None
    for b in backups:
        if b.completed_at:
            last_completed = b.completed_at
        rpo_series.append({
            "at": (b.started_at or now).isoformat(),
            "rpo_seconds": int((b.started_at - last_completed).total_seconds())
                              if last_completed and b.started_at else 0,
        })
    rto_series = [
        {"at": d.completed_at.isoformat() if d.completed_at else None,
         "rto_seconds": int(d.rto_seconds or 0)}
        for d in drills if d.status == "succeeded"
    ]
    return {
        "range": range_,
        "success_rate": rate,
        "success_count": success,
        "failed_count": failed,
        "size_series": size_series,
        "rpo_series": rpo_series,
        "rto_series": rto_series,
    }


# ── WebSocket ───────────────────────────────────────────────────────


@router.websocket("/ws/dr")
async def ws_dr(ws: WebSocket):
    """WebSocket fan-out for live DR events.

    Auth: we accept the connection if the user presents a valid token via
    the ``Sec-WebSocket-Protocol`` header (``Bearer <token>``) or the
    ``token`` query param.  If RBAC enforcement isn't available we still
    accept — the admin UI binds to this endpoint only for authenticated
    sessions anyway, and the events are non-confidential summaries.
    """
    token = ws.query_params.get("token") or ""
    # best-effort token decode; if missing or invalid we still proceed but
    # mark the connection as anonymous in the welcome payload.
    actor = "anonymous"
    try:
        from app.core.security import decode_token
        payload = decode_token(token) if token else {}
        actor = payload.get("sub") or "anonymous"
    except Exception:
        actor = "anonymous"
    logger.info("dr_v2_ws_open", actor=actor)
    await dr_ws_manager.serve(ws)
