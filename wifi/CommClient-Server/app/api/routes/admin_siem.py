"""
SIEM / Audit Chain Dashboard — admin-grade REST + WebSocket surface.

Mounted at ``/api/admin/audit``. Every endpoint requires the ``admin``
role (via ``require_role``); destructive operations also create their
own audit-trail entries via ``audit_log()``.

Routes
------
GET    /head                          — chain head + verification status
GET    /entries                       — paginated entries (cursor-based)
POST   /verify                        — full or scoped chain verification
GET    /verify/jobs/{job_id}          — verify job status
GET    /stats                         — counts & distributions
POST   /export                        — start async export job
GET    /exports/{job_id}              — export job status
GET    /exports/{job_id}/download     — stream the signed bundle
GET    /actors/suggest                — actor autocomplete
WS     /ws                            — live entry + alert stream

Alert Rules
-----------
GET    /rules                          — list
POST   /rules                          — create
PUT    /rules/{rule_id}
DELETE /rules/{rule_id}
POST   /rules/{rule_id}/test           — dry-run against history
POST   /rules/{rule_id}/enable
POST   /rules/{rule_id}/disable

Legal Holds
-----------
GET    /holds
POST   /holds
PUT    /holds/{hold_id}
POST   /holds/{hold_id}/release

Retention
---------
GET    /retention/policies
POST   /retention/policies
PUT    /retention/policies/{policy_id}
DELETE /retention/policies/{policy_id}
POST   /retention/policies/{policy_id}/preview
POST   /retention/policies/{policy_id}/apply
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.core.security import decode_token
from app.core.security_utils import require_role
from app.db.session import async_session_factory
from app.models.audit_alert_rule import (
    AuditAlertRule,
    VALID_ALERT_CHANNELS,
    VALID_RULE_SEVERITIES,
)
from app.models.audit_export_job import VALID_EXPORT_FORMATS
from app.models.legal_hold import LegalHold
from app.models.retention_policy import RetentionPolicy, VALID_RETENTION_ACTIONS
from app.services.audit import audit_search as siem_search
from app.services.audit.alert_rules import (
    AlertRulesEngine,
    CompiledRule,
    DSLError,
    get_engine as get_rules_engine,
    parse_dsl,
)
from app.services.audit.chain import (
    GENESIS_HASH,
    get_audit_chain,
    head_info,
)
from app.services.audit.export_engine import get_export_engine
from app.services.audit.legal_hold import (
    LegalHoldConflict,
    get_legal_hold_service,
)
from app.services.audit.retention import get_retention_service
from app.services.audit.ws_stream import get_ws_manager

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/audit", tags=["admin-siem"])


# ── Pydantic models ──────────────────────────────────────────────────────


class VerifyRequest(BaseModel):
    from_seq: Optional[int] = None
    to_seq: Optional[int] = None
    async_run: bool = False


class ExportRequest(BaseModel):
    scope: dict[str, Any] = Field(default_factory=dict)
    format: str
    filters: dict[str, Any] = Field(default_factory=dict)


class AlertRuleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    condition_dsl: str
    severity: str = "medium"
    channels: list[str] = Field(default_factory=lambda: ["local"])
    enabled: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    condition_dsl: Optional[str] = None
    severity: Optional[str] = None
    channels: Optional[list[str]] = None
    enabled: Optional[bool] = None
    extra: Optional[dict[str, Any]] = None


class AlertRuleTest(BaseModel):
    sample_limit: int = 500


class LegalHoldCreate(BaseModel):
    name: str
    case_ref: Optional[str] = None
    description: Optional[str] = None
    scope: dict[str, Any] = Field(default_factory=dict)
    ends_at: Optional[datetime] = None
    force: bool = False


class LegalHoldUpdate(BaseModel):
    case_ref: Optional[str] = None
    description: Optional[str] = None
    scope: Optional[dict[str, Any]] = None
    ends_at: Optional[datetime] = None


class LegalHoldReleaseBody(BaseModel):
    reason: str
    confirmation: Optional[str] = None


class RetentionCreate(BaseModel):
    name: str
    resource_type: str
    period_days: int
    action: str = "archive"
    exemptions: dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None


class RetentionUpdate(BaseModel):
    name: Optional[str] = None
    resource_type: Optional[str] = None
    period_days: Optional[int] = None
    action: Optional[str] = None
    exemptions: Optional[dict[str, Any]] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


# ── In-memory verify job tracker ─────────────────────────────────────────


_verify_jobs: dict[str, dict[str, Any]] = {}


async def _verify_full(job_id: str) -> None:
    chain = get_audit_chain()
    if chain is None:
        _verify_jobs[job_id].update({"status": "failed",
                                     "error": "chain not configured"})
        return
    _verify_jobs[job_id]["status"] = "running"
    _verify_jobs[job_id]["started_at"] = time.time()
    try:
        ok, broken_at, msg = await asyncio.to_thread(chain.verify)
        _verify_jobs[job_id].update({
            "status": "ready",
            "ok": ok,
            "broken_at_index": broken_at,
            "broken_reason": None if ok else msg,
            "completed_at": time.time(),
        })
    except Exception as exc:
        _verify_jobs[job_id].update({
            "status": "failed",
            "error": str(exc),
        })


# ── Rule cache wiring ────────────────────────────────────────────────────


async def _load_rules_into_engine() -> AlertRulesEngine:
    engine = get_rules_engine()
    async with async_session_factory() as db:
        res = await db.execute(select(AuditAlertRule))
        rows = res.scalars().all()
    compiled: list[CompiledRule] = []
    for r in rows:
        try:
            ast = parse_dsl(r.condition_dsl)
        except DSLError:
            continue
        compiled.append(CompiledRule(
            id=r.id, name=r.name, severity=r.severity,
            channels=list(r.channels or []),
            ast=ast, enabled=bool(r.enabled),
            hit_count=int(r.hit_count or 0),
            raw_dsl=r.condition_dsl,
        ))
    engine.set_rules(compiled)
    return engine


# ── Core endpoints ───────────────────────────────────────────────────────


@router.get("/head")
async def head(
    verify: bool = Query(False, description="walk chain to set verify_status"),
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    info = head_info(verify=verify)
    return info


@router.get("/entries")
async def entries(
    cursor: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    from_ts: Optional[float] = Query(None, alias="from"),
    to_ts: Optional[float] = Query(None, alias="to"),
    actor: Optional[str] = None,
    action: Optional[str] = None,
    resource: Optional[str] = None,
    severity: Optional[str] = None,
    q: Optional[str] = None,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    return siem_search.query_entries(
        cursor=cursor, limit=limit,
        from_ts=from_ts, to_ts=to_ts,
        actor=actor, action=action, resource=resource,
        severity=severity, q=q,
    )


@router.post("/verify")
async def verify(
    body: VerifyRequest,
    bg: BackgroundTasks,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    chain = get_audit_chain()
    if chain is None:
        raise HTTPException(status_code=503, detail="chain not configured")

    # Decide sync vs async based on chain size
    head = chain.head()
    n = head.seq if head else 0
    run_async = body.async_run or n > 10_000

    if run_async:
        job_id = uuid.uuid4().hex
        _verify_jobs[job_id] = {
            "id": job_id, "status": "queued",
            "scope": {"from": body.from_seq, "to": body.to_seq},
        }
        bg.add_task(_verify_full, job_id)
        audit_log("siem.verify.queued", user_id=user_id, success=True,
                  details={"job_id": job_id, "size": n})
        return {"async": True, "job_id": job_id, "status": "queued",
                "estimated_entries": n}

    ok, broken_at, msg = await asyncio.to_thread(chain.verify)
    audit_log("siem.verify.run", user_id=user_id, success=ok,
              details={"broken_at": broken_at, "msg": msg})
    return {
        "async": False,
        "ok": ok,
        "verified_count": n if ok else (broken_at or 0),
        "broken_at_index": broken_at,
        "broken_reason": None if ok else msg,
    }


@router.get("/verify/jobs/{job_id}")
async def verify_job(
    job_id: str,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    job = _verify_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/stats")
async def stats(
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    return siem_search.stats()


@router.get("/actors/suggest")
async def actors_suggest(
    q: str = Query("", min_length=0, max_length=64),
    limit: int = Query(20, ge=1, le=100),
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    return {"suggestions": siem_search.suggest_actors(q, limit=limit)}


# ── Export ──────────────────────────────────────────────────────────────


@router.post("/export", status_code=status.HTTP_202_ACCEPTED)
async def export_start(
    body: ExportRequest,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    if body.format not in VALID_EXPORT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of {VALID_EXPORT_FORMATS}",
        )
    engine = get_export_engine()
    job_id = await engine.start_export(
        scope=body.scope, format=body.format,
        filters=body.filters, actor_id=user_id,
    )
    audit_log("siem.export.start", user_id=user_id, success=True,
              details={"job_id": job_id, "format": body.format})
    return {"job_id": job_id, "status": "queued"}


@router.get("/exports/{job_id}")
async def export_status(
    job_id: str,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    engine = get_export_engine()
    job = await engine.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="export job not found")
    job["download_url"] = (
        f"/api/admin/audit/exports/{job_id}/download"
        if job.get("status") == "ready" else None
    )
    return job


@router.get("/exports/{job_id}/download")
async def export_download(
    job_id: str,
    user_id: str = Depends(require_role("admin")),
):
    engine = get_export_engine()
    job = await engine.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="export job not found")
    if job.get("status") != "ready":
        raise HTTPException(status_code=409, detail="export not ready")
    path = job.get("file_path")
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=410, detail="bundle missing")
    audit_log("siem.export.download", user_id=user_id, success=True,
              details={"job_id": job_id})
    fmt = job.get("format", "")
    media = {
        "csv": "text/csv",
        "pdf": "application/pdf",
        "zip-verifier": "application/zip",
    }.get(fmt, "application/x-jsonlines")
    return FileResponse(
        path, media_type=media,
        filename=f"helen-audit-{job_id[:8]}.{Path(path).suffix.lstrip('.')}",
        headers={"X-Audit-HMAC": job.get("hmac_signature") or ""},
    )


# ── Alert rules ─────────────────────────────────────────────────────────


@router.get("/rules")
async def rules_list(
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    async with async_session_factory() as db:
        res = await db.execute(select(AuditAlertRule).order_by(
            AuditAlertRule.created_at.desc()))
        rows = res.scalars().all()
    return {"rules": [r.to_dict() for r in rows]}


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def rules_create(
    body: AlertRuleCreate,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    if body.severity not in VALID_RULE_SEVERITIES:
        raise HTTPException(status_code=400,
                            detail=f"severity must be one of {VALID_RULE_SEVERITIES}")
    for ch in body.channels:
        if ch not in VALID_ALERT_CHANNELS:
            raise HTTPException(status_code=400,
                                detail=f"channel {ch!r} invalid")
    try:
        parse_dsl(body.condition_dsl)
    except DSLError as e:
        raise HTTPException(status_code=400, detail=f"DSL error: {e}")

    async with async_session_factory() as db:
        row = AuditAlertRule(
            name=body.name,
            description=body.description,
            condition_dsl=body.condition_dsl,
            severity=body.severity,
            channels=list(body.channels),
            enabled=body.enabled,
            created_by=user_id,
            extra=dict(body.extra or {}),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        result = row.to_dict()

    await _load_rules_into_engine()
    audit_log("siem.rule.create", user_id=user_id, success=True,
              details={"rule_id": result["id"], "name": body.name})
    return result


@router.put("/rules/{rule_id}")
async def rules_update(
    rule_id: str,
    body: AlertRuleUpdate,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    if body.condition_dsl:
        try:
            parse_dsl(body.condition_dsl)
        except DSLError as e:
            raise HTTPException(status_code=400, detail=f"DSL error: {e}")

    async with async_session_factory() as db:
        res = await db.execute(select(AuditAlertRule).where(
            AuditAlertRule.id == rule_id))
        row = res.scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="rule not found")
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(row, k, v)
        await db.commit()
        await db.refresh(row)
        result = row.to_dict()

    await _load_rules_into_engine()
    audit_log("siem.rule.update", user_id=user_id, success=True,
              details={"rule_id": rule_id})
    return result


@router.delete("/rules/{rule_id}")
async def rules_delete(
    rule_id: str,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    async with async_session_factory() as db:
        res = await db.execute(select(AuditAlertRule).where(
            AuditAlertRule.id == rule_id))
        row = res.scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="rule not found")
        await db.delete(row)
        await db.commit()
    await _load_rules_into_engine()
    audit_log("siem.rule.delete", user_id=user_id, success=True,
              details={"rule_id": rule_id})
    return {"ok": True, "deleted": rule_id}


@router.post("/rules/{rule_id}/test")
async def rules_test(
    rule_id: str,
    body: AlertRuleTest = AlertRuleTest(),
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    async with async_session_factory() as db:
        res = await db.execute(select(AuditAlertRule).where(
            AuditAlertRule.id == rule_id))
        row = res.scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="rule not found")
        dsl = row.condition_dsl
    try:
        ast = parse_dsl(dsl)
    except DSLError as e:
        raise HTTPException(status_code=400, detail=f"DSL error: {e}")

    chain = get_audit_chain()
    if chain is None:
        return {"scanned": 0, "matched_count": 0, "samples": []}
    sample = list(chain.filter(limit=body.sample_limit))
    engine = get_rules_engine()
    return engine.dry_run(ast, sample)


@router.post("/rules/{rule_id}/enable")
async def rules_enable(
    rule_id: str,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    return await _toggle_rule(rule_id, enabled=True, actor_id=user_id)


@router.post("/rules/{rule_id}/disable")
async def rules_disable(
    rule_id: str,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    return await _toggle_rule(rule_id, enabled=False, actor_id=user_id)


async def _toggle_rule(rule_id: str, *, enabled: bool, actor_id: str) -> dict[str, Any]:
    async with async_session_factory() as db:
        res = await db.execute(select(AuditAlertRule).where(
            AuditAlertRule.id == rule_id))
        row = res.scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="rule not found")
        row.enabled = enabled
        await db.commit()
        await db.refresh(row)
        result = row.to_dict()
    await _load_rules_into_engine()
    audit_log(f"siem.rule.{'enable' if enabled else 'disable'}",
              user_id=actor_id, success=True, details={"rule_id": rule_id})
    return result


# ── Legal holds ─────────────────────────────────────────────────────────


@router.get("/holds")
async def holds_list(
    status_filter: Optional[str] = Query(None, alias="status"),
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    svc = get_legal_hold_service()
    return {"holds": await svc.list(status=status_filter)}


@router.post("/holds", status_code=status.HTTP_201_CREATED)
async def holds_create(
    body: LegalHoldCreate,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    svc = get_legal_hold_service()
    try:
        return await svc.create(
            name=body.name, scope=body.scope, actor_id=user_id,
            case_ref=body.case_ref, description=body.description,
            ends_at=body.ends_at, force=body.force,
        )
    except LegalHoldConflict as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.put("/holds/{hold_id}")
async def holds_update(
    hold_id: str,
    body: LegalHoldUpdate,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    svc = get_legal_hold_service()
    result = await svc.update(
        hold_id, actor_id=user_id, **body.model_dump(exclude_none=True),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="hold not found")
    return result


@router.post("/holds/{hold_id}/release")
async def holds_release(
    hold_id: str,
    body: LegalHoldReleaseBody,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    svc = get_legal_hold_service()
    try:
        result = await svc.release(
            hold_id, actor_id=user_id, reason=body.reason,
            confirmation=body.confirmation,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="hold not found")
    return result


# ── Retention ───────────────────────────────────────────────────────────


@router.get("/retention/policies")
async def retention_list(
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    svc = get_retention_service()
    return {"policies": await svc.list_policies()}


@router.post("/retention/policies", status_code=status.HTTP_201_CREATED)
async def retention_create(
    body: RetentionCreate,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    if body.action not in VALID_RETENTION_ACTIONS:
        raise HTTPException(status_code=400,
                            detail=f"action must be in {VALID_RETENTION_ACTIONS}")
    svc = get_retention_service()
    return await svc.create(
        name=body.name, resource_type=body.resource_type,
        period_days=body.period_days, action=body.action,
        actor_id=user_id, exemptions=body.exemptions,
        description=body.description,
    )


@router.put("/retention/policies/{policy_id}")
async def retention_update(
    policy_id: str,
    body: RetentionUpdate,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    svc = get_retention_service()
    result = await svc.update(
        policy_id, actor_id=user_id, **body.model_dump(exclude_none=True),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return result


@router.delete("/retention/policies/{policy_id}")
async def retention_delete(
    policy_id: str,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    svc = get_retention_service()
    ok = await svc.delete(policy_id, actor_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="policy not found")
    return {"ok": True, "deleted": policy_id}


@router.post("/retention/policies/{policy_id}/preview")
async def retention_preview(
    policy_id: str,
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    svc = get_retention_service()
    try:
        return await svc.preview(policy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="policy not found")


@router.post("/retention/policies/{policy_id}/apply")
async def retention_apply(
    policy_id: str,
    dry_run: bool = Query(False),
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    svc = get_retention_service()
    try:
        return await svc.apply(policy_id, actor_id=user_id, dry_run=dry_run)
    except KeyError:
        raise HTTPException(status_code=404, detail="policy not found")


# ── WebSocket ───────────────────────────────────────────────────────────


@router.websocket("/ws")
async def ws_audit(websocket: WebSocket, token: Optional[str] = Query(None)) -> None:
    """Live audit stream. Auth via ?token=<JWT>; the JWT must carry the
    admin role. Optional filter query params: actor, action, resource."""
    if not token:
        await websocket.close(code=4401)
        return
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            await websocket.close(code=4401)
            return
        if payload.get("role") not in ("admin", "moderator"):
            await websocket.close(code=4403)
            return
    except Exception:
        await websocket.close(code=4401)
        return

    filters: dict[str, Any] = {}
    for k in ("actor", "action", "resource"):
        v = websocket.query_params.get(k)
        if v:
            filters[k] = v

    # Ensure the engine cache is populated for live alert evaluation
    try:
        await _load_rules_into_engine()
    except Exception as exc:
        logger.debug("rules_engine_load_failed", error=str(exc))

    mgr = get_ws_manager()
    sub = await mgr.connect(websocket, filters)
    try:
        await mgr.pump(sub)
    except WebSocketDisconnect:
        await mgr.disconnect(sub)
