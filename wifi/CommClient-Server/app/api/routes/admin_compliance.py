"""
Phase 6 / Module AB — admin compliance endpoints.

Mounted under ``/api/admin/compliance``. Every route requires the
``compliance.manage`` permission.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.compliance import (
    VALID_PII_CLASSIFICATIONS,
    VALID_RETENTION_ACTIONS,
    ConsentRecord,
    DataDeletionRequest,
    DataExportRequest,
    PIIInventoryEntry,
    RetentionPolicy,
)
from app.services.compliance import (
    pii_classifier,
    reports as report_gen,
    retention,
)
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin/compliance", tags=["admin-compliance"])

_PERM = "compliance.manage"


# ── exports / deletions ─────────────────────────────────────────


@router.get("/exports")
async def list_exports(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(DataExportRequest).order_by(desc(DataExportRequest.requested_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "items": [
            {
                "id": r.id, "user_id": r.user_id, "status": r.status,
                "requested_at": r.requested_at.isoformat() if r.requested_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "size_bytes": r.size_bytes,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            } for r in rows
        ],
    }


@router.get("/deletions")
async def list_deletions(
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = select(DataDeletionRequest)
    if status_filter:
        q = q.where(DataDeletionRequest.status == status_filter)
    q = q.order_by(desc(DataDeletionRequest.requested_at))
    q = q.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return {
        "items": [
            {
                "id": r.id, "user_id": r.user_id, "status": r.status,
                "requested_at": r.requested_at.isoformat() if r.requested_at else None,
                "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
                "executed_at": r.executed_at.isoformat() if r.executed_at else None,
                "rows_affected": r.dry_run_report.get("total_rows_touched") if r.dry_run_report else None,
            } for r in rows
        ],
    }


# ── retention policies ──────────────────────────────────────────


class RetentionPolicyIn(BaseModel):
    entity_type: str
    retention_days: int = Field(ge=1, le=36500)
    action: str = "delete"
    enabled: bool = True


@router.get("/retention-policies")
async def list_retention_policies(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(select(RetentionPolicy))).scalars().all()
    return {
        "known_entity_types": retention.known_entity_types(),
        "items": [
            {
                "id": p.id, "entity_type": p.entity_type,
                "retention_days": p.retention_days, "action": p.action,
                "enabled": p.enabled,
                "last_run_at": p.last_run_at.isoformat() if p.last_run_at else None,
                "last_run_affected": p.last_run_affected,
            } for p in rows
        ],
        "scheduler": {
            "enabled": retention.get_state().enabled,
            "interval_hours": retention.get_state().interval_hours,
            "last_run": (
                retention.get_state().last_run.isoformat()
                if retention.get_state().last_run else None
            ),
        },
    }


@router.post("/retention-policies")
async def upsert_retention_policy(
    body: RetentionPolicyIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.action not in VALID_RETENTION_ACTIONS:
        raise HTTPException(400, detail=f"action must be one of {VALID_RETENTION_ACTIONS}")
    existing = (await db.execute(
        select(RetentionPolicy).where(RetentionPolicy.entity_type == body.entity_type)
    )).scalar_one_or_none()
    if existing:
        existing.retention_days = body.retention_days
        existing.action = body.action
        existing.enabled = body.enabled
    else:
        existing = RetentionPolicy(
            id=uuid.uuid4().hex,
            entity_type=body.entity_type,
            retention_days=body.retention_days,
            action=body.action, enabled=body.enabled,
        )
        db.add(existing)
    await db.commit()
    audit_log("compliance.retention_policy_set", user_id=user_id, success=True,
              details={"entity_type": body.entity_type, "days": body.retention_days,
                       "action": body.action, "enabled": body.enabled})
    return {"id": existing.id, "entity_type": existing.entity_type}


@router.post("/retention/run-now")
async def retention_run_now(
    dry_run: bool = Query(False),
    user_id: str = Depends(require_permission(_PERM)),
):
    res = await retention.run_pass(dry_run=dry_run)
    audit_log("compliance.retention_run_now", user_id=user_id, success=True,
              details={"dry_run": dry_run, "results": res})
    return {"results": res, "dry_run": dry_run}


# ── PII inventory ───────────────────────────────────────────────


@router.get("/pii-inventory")
async def get_pii_inventory(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(select(PIIInventoryEntry))).scalars().all()
    return {
        "items": [
            {
                "id": r.id, "table_name": r.table_name,
                "column_name": r.column_name, "classification": r.classification,
                "encryption_status": r.encryption_status,
                "masking_rule": r.masking_rule, "notes": r.notes,
            } for r in rows
        ],
        "heatmap": await pii_classifier.heatmap(),
    }


@router.post("/pii-inventory/rebuild")
async def rebuild_pii_inventory(
    user_id: str = Depends(require_permission(_PERM)),
):
    res = await pii_classifier.rebuild_inventory()
    audit_log("compliance.pii_rebuild", user_id=user_id, success=True, details=res)
    return res


class PIIPatchIn(BaseModel):
    classification: Optional[str] = None
    encryption_status: Optional[str] = None
    masking_rule: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/pii-inventory/{entry_id}")
async def patch_pii_entry(
    entry_id: str,
    body: PIIPatchIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(PIIInventoryEntry).where(PIIInventoryEntry.id == entry_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="entry not found")
    if body.classification:
        if body.classification not in VALID_PII_CLASSIFICATIONS:
            raise HTTPException(400, detail="invalid classification")
        row.classification = body.classification
    if body.encryption_status is not None:
        row.encryption_status = body.encryption_status
    if body.masking_rule is not None:
        row.masking_rule = body.masking_rule
    if body.notes is not None:
        row.notes = body.notes
    await db.commit()
    audit_log("compliance.pii_entry_patched", user_id=user_id, success=True,
              details={"entry_id": entry_id})
    return {"id": row.id}


# ── reports ─────────────────────────────────────────────────────


@router.post("/reports/{kind}")
async def generate_report(
    kind: str,
    period_days: int = Query(90, ge=1, le=3650),
    user_id: str = Depends(require_permission(_PERM)),
):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=period_days)
    if kind == "soc2":
        res = await report_gen.generate_soc2_report(start, end)
    elif kind == "gdpr":
        res = await report_gen.generate_gdpr_report()
    elif kind == "hipaa":
        res = await report_gen.generate_hipaa_log(start, end)
    else:
        raise HTTPException(400, detail="kind must be one of: soc2, gdpr, hipaa")
    audit_log("compliance.report_generated", user_id=user_id, success=True,
              details={"kind": kind, "id": res.get("id")})
    return res


# ── audit summary ───────────────────────────────────────────────


@router.get("/audit-summary")
async def audit_summary(
    period: str = Query("30d"),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    """High-level counts for the dashboard."""
    days = 30
    if period.endswith("d"):
        try:
            days = int(period[:-1])
        except ValueError:
            pass
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    exports = (await db.execute(
        select(func.count()).select_from(DataExportRequest)
        .where(DataExportRequest.requested_at >= cutoff)
    )).scalar_one()
    deletions = (await db.execute(
        select(func.count()).select_from(DataDeletionRequest)
        .where(DataDeletionRequest.requested_at >= cutoff)
    )).scalar_one()
    consents = (await db.execute(
        select(func.count()).select_from(ConsentRecord)
        .where(ConsentRecord.granted_at >= cutoff)
    )).scalar_one()
    return {
        "period_days": days,
        "exports": int(exports or 0),
        "deletions": int(deletions or 0),
        "consents": int(consents or 0),
    }


# ─────────────────────────────────────────────────────────────────
# COMPLIANCE / eDISCOVERY WORKBENCH — Phase 6 part B
#
# Endpoints below augment the legacy AB surface with the full
# Workbench feature set: legal holds, advanced retention, eDiscovery
# search + cases, DSAR (Art. 15) / RTBF (Art. 17), classification
# rules + scans, framework posture, signed reports.
#
# All routes are guarded by the same `_PERM` permission and write
# audit-chain entries on every mutating action.
# ─────────────────────────────────────────────────────────────────

from fastapi import status as _http_status                              # noqa: E402

from app.models.compliance_case import (                                # noqa: E402
    ComplianceCase,
    ComplianceCaseEvidence,
    ComplianceCaseExport,
    VALID_CASE_STATUSES,
    VALID_EVIDENCE_TAGS,
)
from app.models.compliance_classification import (                     # noqa: E402
    ClassificationFinding,
    ClassificationRule,
    VALID_RULE_ACTIONS,
    VALID_RULE_KINDS,
)
from app.models.compliance_dsar import (                               # noqa: E402
    DSARRequest,
    VALID_DSAR_TYPES,
)
from app.models.compliance_hold import (                               # noqa: E402
    ComplianceHold,
    ComplianceHoldAudit,
    VALID_HOLD_STATUSES,
)
from app.models.compliance_report import (                             # noqa: E402
    ComplianceReport,
    ComplianceReportSchedule,
    VALID_FRAMEWORKS,
    VALID_REPORT_FORMATS,
)
from app.models.compliance_retention import (                          # noqa: E402
    ComplianceRetentionJob,
    ComplianceRetentionPolicy,
    VALID_ADV_RETENTION_ACTIONS,
)
from app.models.compliance_rtbf import RTBFRequest                     # noqa: E402
from app.services.compliance.case_export import case_exporter          # noqa: E402
from app.services.compliance.classification import classification_service  # noqa: E402
from app.services.compliance.dsar import dsar_service                  # noqa: E402
from app.services.compliance.ediscovery_engine import ediscovery_engine  # noqa: E402
from app.services.compliance.framework_engine import framework_engine  # noqa: E402
from app.services.compliance.legal_holds import legal_holds_service    # noqa: E402
from app.services.compliance.report_generator import report_generator  # noqa: E402
from app.services.compliance.retention import retention_service_v2     # noqa: E402
from app.services.compliance.rtbf import (                             # noqa: E402
    RTBFConflictError,
    rtbf_service,
)


# ── helpers ─────────────────────────────────────────────────────


def _hold_to_dict(h: ComplianceHold) -> Dict[str, Any]:
    return {
        "id": h.id, "name": h.name, "case_ref": h.case_ref,
        "description": h.description, "scope": h.scope,
        "status": h.status, "retention_override": h.retention_override,
        "notify": h.notify, "created_by": h.created_by,
        "created_at": h.created_at.isoformat() if h.created_at else None,
        "expires_at": h.expires_at.isoformat() if h.expires_at else None,
        "released_at": h.released_at.isoformat() if h.released_at else None,
        "released_by": h.released_by, "release_reason": h.release_reason,
    }


def _case_to_dict(c: ComplianceCase) -> Dict[str, Any]:
    return {
        "id": c.id, "name": c.name, "matter_number": c.matter_number,
        "description": c.description, "status": c.status,
        "owner_id": c.owner_id, "custodians": c.custodians or [],
        "hold_id": c.hold_id, "evidence_count": c.evidence_count,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "closed_at": c.closed_at.isoformat() if c.closed_at else None,
    }


def _policy_v2_to_dict(p: ComplianceRetentionPolicy) -> Dict[str, Any]:
    return {
        "id": p.id, "name": p.name, "description": p.description,
        "resource_type": p.resource_type, "selector": p.selector,
        "retention_days": p.retention_days, "action": p.action,
        "enabled": p.enabled, "respect_legal_hold": p.respect_legal_hold,
        "last_run_at": p.last_run_at.isoformat() if p.last_run_at else None,
        "last_run_affected": p.last_run_affected,
        "last_run_skipped_held": p.last_run_skipped_held,
    }


# ── HOLDS ───────────────────────────────────────────────────────


class HoldCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    case_ref: Optional[str] = None
    description: Optional[str] = None
    scope: Dict[str, Any] = Field(default_factory=dict)
    retention_override: bool = True
    notify: bool = False
    expires_at: Optional[datetime] = None


class HoldScopeIn(BaseModel):
    scope: Dict[str, Any]


class HoldReleaseIn(BaseModel):
    confirmation: str
    reason: str


@router.get("/holds")
async def list_holds(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    if status and status not in VALID_HOLD_STATUSES:
        raise HTTPException(400, detail=f"status must be one of {VALID_HOLD_STATUSES}")
    rows = await legal_holds_service.list_holds(
        db, status=status, search=search, limit=limit, offset=offset,
    )
    return {"items": [_hold_to_dict(h) for h in rows]}


@router.post("/holds", status_code=_http_status.HTTP_201_CREATED)
async def create_hold(
    body: HoldCreateIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    hold, conflicts = await legal_holds_service.create(
        db, name=body.name, case_ref=body.case_ref, description=body.description,
        scope=body.scope, retention_override=body.retention_override,
        notify=body.notify, expires_at=body.expires_at, actor_id=user_id,
    )
    return {
        "hold": _hold_to_dict(hold),
        "conflicts": [_hold_to_dict(c) for c in conflicts],
    }


@router.get("/holds/{hold_id}")
async def get_hold(
    hold_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    h = await legal_holds_service.get(db, hold_id)
    if h is None:
        raise HTTPException(404, detail="hold not found")
    return _hold_to_dict(h)


@router.put("/holds/{hold_id}/scope")
async def update_hold_scope(
    hold_id: str,
    body: HoldScopeIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        h = await legal_holds_service.update_scope(
            db, hold_id=hold_id, scope=body.scope, actor_id=user_id,
        )
    except LookupError:
        raise HTTPException(404, detail="hold not found")
    except ValueError as e:
        raise HTTPException(409, detail=str(e))
    return _hold_to_dict(h)


@router.post("/holds/{hold_id}/release")
async def release_hold(
    hold_id: str,
    body: HoldReleaseIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.confirmation != "RELEASE":
        raise HTTPException(400, detail="typed confirmation must be 'RELEASE'")
    if not body.reason or len(body.reason.strip()) < 3:
        raise HTTPException(400, detail="reason required")
    try:
        h = await legal_holds_service.release(
            db, hold_id=hold_id, reason=body.reason, actor_id=user_id,
        )
    except LookupError:
        raise HTTPException(404, detail="hold not found")
    except ValueError as e:
        raise HTTPException(409, detail=str(e))
    return _hold_to_dict(h)


@router.get("/holds/{hold_id}/audit")
async def hold_audit(
    hold_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    h = await legal_holds_service.get(db, hold_id)
    if h is None:
        raise HTTPException(404, detail="hold not found")
    rows = await legal_holds_service.audit_trail(db, hold_id)
    return {
        "hold_id": hold_id,
        "items": [
            {"event": r.event, "actor_id": r.actor_id,
             "occurred_at": r.occurred_at.isoformat(),
             "details": r.details}
            for r in rows
        ],
    }


# ── RETENTION v2 ────────────────────────────────────────────────


class RetentionV2In(BaseModel):
    name: str
    resource_type: str
    retention_days: int = Field(ge=1, le=36500)
    action: str = "delete"
    selector: Dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    respect_legal_hold: bool = True
    enabled: bool = True


class RetentionV2Patch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    retention_days: Optional[int] = Field(default=None, ge=1, le=36500)
    action: Optional[str] = None
    selector: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    respect_legal_hold: Optional[bool] = None


class RetentionPreviewIn(BaseModel):
    policy: Dict[str, Any]


class RetentionApplyIn(BaseModel):
    confirmation: str
    dry_run: bool = False


class RetentionRunAllIn(BaseModel):
    confirmation: str


@router.get("/retention/policies")
async def retention_v2_list(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = await retention_service_v2.list_policies(db)
    return {"items": [_policy_v2_to_dict(p) for p in rows],
            "valid_actions": list(VALID_ADV_RETENTION_ACTIONS)}


@router.post("/retention/policies", status_code=_http_status.HTTP_201_CREATED)
async def retention_v2_create(
    body: RetentionV2In,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        p = await retention_service_v2.create_policy(
            db, name=body.name, resource_type=body.resource_type,
            retention_days=body.retention_days, action=body.action,
            selector=body.selector, description=body.description,
            respect_legal_hold=body.respect_legal_hold,
            enabled=body.enabled, actor_id=user_id,
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return _policy_v2_to_dict(p)


@router.put("/retention/policies/{policy_id}")
async def retention_v2_update(
    policy_id: str,
    body: RetentionV2Patch,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        p = await retention_service_v2.update_policy(
            db, policy_id, patch=body.dict(exclude_unset=True), actor_id=user_id,
        )
    except LookupError:
        raise HTTPException(404, detail="policy not found")
    return _policy_v2_to_dict(p)


@router.delete("/retention/policies/{policy_id}")
async def retention_v2_delete(
    policy_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        await retention_service_v2.delete_policy(db, policy_id, actor_id=user_id)
    except LookupError:
        raise HTTPException(404, detail="policy not found")
    return {"ok": True}


@router.post("/retention/policies/preview")
async def retention_v2_preview(
    body: RetentionPreviewIn,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    return await retention_service_v2.preview(db, body.policy)


@router.post("/retention/policies/{policy_id}/apply")
async def retention_v2_apply(
    policy_id: str,
    body: RetentionApplyIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.confirmation != "APPLY":
        raise HTTPException(400, detail="typed confirmation must be 'APPLY'")
    try:
        result = await retention_service_v2.apply(
            db, policy_id, dry_run=body.dry_run, actor_id=user_id,
        )
    except LookupError:
        raise HTTPException(404, detail="policy not found")
    return result


@router.post("/retention/run_all")
async def retention_v2_run_all(
    body: RetentionRunAllIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.confirmation != "RUN":
        raise HTTPException(400, detail="typed confirmation must be 'RUN'")
    return {"jobs": await retention_service_v2.run_all(db, actor_id=user_id)}


# ── eDISCOVERY ──────────────────────────────────────────────────


class EDSearchIn(BaseModel):
    q: str = ""
    filters: Dict[str, Any] = Field(default_factory=dict)
    sort: str = "relevance"
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class CaseCreateIn(BaseModel):
    name: str
    matter_number: Optional[str] = None
    description: Optional[str] = None
    custodians: List[str] = Field(default_factory=list)
    hold_id: Optional[str] = None


class CasePatchIn(BaseModel):
    name: Optional[str] = None
    matter_number: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    custodians: Optional[List[str]] = None
    hold_id: Optional[str] = None


class EvidenceItemIn(BaseModel):
    resource_type: str
    resource_id: str
    tag: str = "relevant"
    notes: Optional[str] = None
    snapshot: Optional[Dict[str, Any]] = None


class CaseEvidenceIn(BaseModel):
    items: List[EvidenceItemIn]


class CaseExportIn(BaseModel):
    format: str
    options: Dict[str, Any] = Field(default_factory=dict)


@router.post("/ediscovery/search")
async def ediscovery_search(
    body: EDSearchIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    res = await ediscovery_engine.search(
        db, q=body.q, filters=body.filters, sort=body.sort,
        limit=body.limit, offset=body.offset,
    )
    audit_log("compliance.ediscovery_search", user_id=user_id, success=True,
              details={"q": body.q[:200], "total": res.get("total", 0)})
    return res


@router.get("/ediscovery/cases")
async def cases_list(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = await ediscovery_engine.list_cases(
        db, status=status, search=search, limit=limit, offset=offset,
    )
    return {"items": [_case_to_dict(c) for c in rows]}


@router.post("/ediscovery/cases", status_code=_http_status.HTTP_201_CREATED)
async def cases_create(
    body: CaseCreateIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    c = await ediscovery_engine.create_case(
        db, name=body.name, matter_number=body.matter_number,
        description=body.description, custodians=body.custodians,
        hold_id=body.hold_id, actor_id=user_id,
    )
    return _case_to_dict(c)


@router.put("/ediscovery/cases/{case_id}")
async def cases_update(
    case_id: str,
    body: CasePatchIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.status and body.status not in VALID_CASE_STATUSES:
        raise HTTPException(400, detail=f"status must be one of {VALID_CASE_STATUSES}")
    try:
        c = await ediscovery_engine.update_case(
            db, case_id, patch=body.dict(exclude_unset=True), actor_id=user_id,
        )
    except LookupError:
        raise HTTPException(404, detail="case not found")
    return _case_to_dict(c)


@router.delete("/ediscovery/cases/{case_id}")
async def cases_delete(
    case_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        await ediscovery_engine.delete_case(db, case_id, actor_id=user_id)
    except LookupError:
        raise HTTPException(404, detail="case not found")
    return {"ok": True}


@router.post("/ediscovery/cases/{case_id}/evidence")
async def cases_add_evidence(
    case_id: str,
    body: CaseEvidenceIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    for it in body.items:
        if it.tag not in VALID_EVIDENCE_TAGS:
            raise HTTPException(400, detail=f"tag '{it.tag}' invalid")
    try:
        return await ediscovery_engine.add_evidence(
            db, case_id, items=[i.dict() for i in body.items], actor_id=user_id,
        )
    except LookupError:
        raise HTTPException(404, detail="case not found")


@router.get("/ediscovery/cases/{case_id}/timeline")
async def cases_timeline(
    case_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    return {"items": await ediscovery_engine.case_timeline(db, case_id)}


@router.post("/ediscovery/cases/{case_id}/export")
async def cases_export(
    case_id: str,
    body: CaseExportIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        job = await case_exporter.export(
            db, case_id, format=body.format, options=body.options,
            actor_id=user_id,
        )
    except LookupError:
        raise HTTPException(404, detail="case not found")
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {
        "export_job_id": job.id, "status": job.status,
        "format": job.format, "sha256": job.sha256,
        "size_bytes": job.size_bytes,
    }


@router.get("/ediscovery/cases/{case_id}/exports/{job_id}")
async def cases_export_status(
    case_id: str, job_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    job = (await db.execute(
        select(ComplianceCaseExport)
        .where(ComplianceCaseExport.id == job_id,
               ComplianceCaseExport.case_id == case_id)
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, detail="export not found")
    return {
        "id": job.id, "status": job.status, "format": job.format,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "sha256": job.sha256, "signature": job.signature,
        "size_bytes": job.size_bytes,
        "download_url": (
            f"/api/admin/compliance/ediscovery/cases/{case_id}/exports/{job.id}/download"
            if job.status == "ready" else None
        ),
        "error": job.error_message,
    }


# ── DSAR (Article 15) ───────────────────────────────────────────


class DSARCreateIn(BaseModel):
    subject_id: str
    subject_email: Optional[str] = None
    subject_name: Optional[str] = None
    identity_verified: bool = False
    identity_proof: Optional[Dict[str, Any]] = None
    type: str = "access"
    scope: Dict[str, Any] = Field(default_factory=dict)
    deadline_days: int = 30


class DSARFulfillIn(BaseModel):
    confirmation: str
    redact_pii: bool = False
    response_template: Optional[str] = None


def _dsar_to_dict(r: DSARRequest) -> Dict[str, Any]:
    return {
        "id": r.id, "subject_id": r.subject_id,
        "subject_email": r.subject_email, "subject_name": r.subject_name,
        "request_type": r.request_type, "status": r.status,
        "identity_verified": r.identity_verified,
        "received_at": r.received_at.isoformat() if r.received_at else None,
        "deadline_at": r.deadline_at.isoformat() if r.deadline_at else None,
        "fulfilled_at": r.fulfilled_at.isoformat() if r.fulfilled_at else None,
        "file_path": r.file_path, "sha256": r.sha256,
        "size_bytes": r.size_bytes,
        "error": r.error_message,
    }


@router.get("/dsar/requests")
async def dsar_list(
    status: Optional[str] = Query(None),
    overdue: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = await dsar_service.list_requests(
        db, status=status, overdue=overdue, limit=limit, offset=offset,
    )
    return {"items": [_dsar_to_dict(r) for r in rows]}


@router.post("/dsar/requests", status_code=_http_status.HTTP_201_CREATED)
async def dsar_create(
    body: DSARCreateIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.type not in VALID_DSAR_TYPES:
        raise HTTPException(400, detail=f"type must be one of {VALID_DSAR_TYPES}")
    try:
        r = await dsar_service.create(
            db, subject_id=body.subject_id, subject_email=body.subject_email,
            subject_name=body.subject_name, request_type=body.type,
            identity_verified=body.identity_verified,
            identity_proof=body.identity_proof, scope=body.scope,
            deadline_days=body.deadline_days, actor_id=user_id,
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return _dsar_to_dict(r)


@router.get("/dsar/requests/{dsar_id}")
async def dsar_get(
    dsar_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    r = await dsar_service.get(db, dsar_id)
    if r is None:
        raise HTTPException(404, detail="dsar not found")
    return _dsar_to_dict(r)


@router.post("/dsar/requests/{dsar_id}/fulfill")
async def dsar_fulfill(
    dsar_id: str,
    body: DSARFulfillIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if body.confirmation != "FULFILL":
        raise HTTPException(400, detail="typed confirmation must be 'FULFILL'")
    try:
        return await dsar_service.fulfill(
            db, dsar_id, redact_pii=body.redact_pii,
            response_template=body.response_template, actor_id=user_id,
        )
    except LookupError:
        raise HTTPException(404, detail="dsar not found")
    except PermissionError as e:
        raise HTTPException(403, detail=str(e))


# ── RTBF (Article 17) ───────────────────────────────────────────


class RTBFCreateIn(BaseModel):
    subject_id: str
    subject_email: Optional[str] = None
    justification: Optional[str] = None
    scope: Dict[str, Any] = Field(default_factory=dict)


class RTBFExecuteIn(BaseModel):
    confirmation: str


def _rtbf_to_dict(r: RTBFRequest) -> Dict[str, Any]:
    return {
        "id": r.id, "subject_id": r.subject_id,
        "subject_email": r.subject_email,
        "justification": r.justification, "scope": r.scope,
        "status": r.status, "hold_conflicts": r.hold_conflicts,
        "blocked_reason": r.blocked_reason,
        "received_at": r.received_at.isoformat() if r.received_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "messages_redacted": r.messages_redacted,
        "files_deleted": r.files_deleted,
        "audit_entries_marked": r.audit_entries_marked,
        "verification_report": r.verification_report,
        "error": r.error_message,
    }


@router.get("/rtbf/requests")
async def rtbf_list(
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = await rtbf_service.list_requests(
        db, status=status, limit=limit, offset=offset,
    )
    return {"items": [_rtbf_to_dict(r) for r in rows]}


@router.post("/rtbf/requests", status_code=_http_status.HTTP_201_CREATED)
async def rtbf_create(
    body: RTBFCreateIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    r = await rtbf_service.create(
        db, subject_id=body.subject_id, subject_email=body.subject_email,
        justification=body.justification, scope=body.scope,
        actor_id=user_id,
    )
    # If holds blocked the request, signal 409 to the client
    if r.status == "blocked":
        raise HTTPException(
            status_code=_http_status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "Erasure refused: subject is under legal hold "
                    "(GDPR Article 17(3)(e))."
                ),
                "rtbf_id": r.id,
                "hold_conflicts": r.hold_conflicts,
                "gdpr_article": "17(3)(e)",
            },
        )
    return _rtbf_to_dict(r)


@router.post("/rtbf/requests/{rtbf_id}/execute")
async def rtbf_execute(
    rtbf_id: str,
    body: RTBFExecuteIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        return await rtbf_service.execute(
            db, rtbf_id, confirmation=body.confirmation, actor_id=user_id,
        )
    except LookupError:
        raise HTTPException(404, detail="rtbf not found")
    except RTBFConflictError as e:
        raise HTTPException(
            status_code=_http_status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "Erasure refused: subject is under legal hold "
                    "(GDPR Article 17(3)(e))."
                ),
                "hold_conflicts": e.hold_ids,
                "gdpr_article": "17(3)(e)",
            },
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


# ── CLASSIFICATION ──────────────────────────────────────────────


class ClassificationRuleIn(BaseModel):
    name: str
    kind: str
    pattern: str
    action: str = "tag"
    severity: str = "medium"
    classification: str = "pii"
    description: Optional[str] = None
    enabled: bool = True


class ClassificationRulePatch(BaseModel):
    name: Optional[str] = None
    pattern: Optional[str] = None
    action: Optional[str] = None
    severity: Optional[str] = None
    classification: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


class ClassificationScanIn(BaseModel):
    scope: Dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False


@router.get("/classification/rules")
async def classification_list_rules(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = await classification_service.list_rules(db)
    return {
        "items": [
            {"id": r.id, "name": r.name, "kind": r.kind,
             "pattern": r.pattern, "action": r.action,
             "severity": r.severity, "classification": r.classification,
             "enabled": r.enabled, "is_builtin": r.is_builtin}
            for r in rows
        ],
        "valid_kinds": list(VALID_RULE_KINDS),
        "valid_actions": list(VALID_RULE_ACTIONS),
    }


@router.post("/classification/rules", status_code=_http_status.HTTP_201_CREATED)
async def classification_create_rule(
    body: ClassificationRuleIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        r = await classification_service.create_rule(
            db, name=body.name, kind=body.kind, pattern=body.pattern,
            action=body.action, severity=body.severity,
            classification=body.classification,
            description=body.description, enabled=body.enabled,
            actor_id=user_id,
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {"id": r.id}


@router.put("/classification/rules/{rule_id}")
async def classification_update_rule(
    rule_id: str,
    body: ClassificationRulePatch,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        r = await classification_service.update_rule(
            db, rule_id, patch=body.dict(exclude_unset=True), actor_id=user_id,
        )
    except LookupError:
        raise HTTPException(404, detail="rule not found")
    return {"id": r.id}


@router.delete("/classification/rules/{rule_id}")
async def classification_delete_rule(
    rule_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        await classification_service.delete_rule(db, rule_id, actor_id=user_id)
    except LookupError:
        raise HTTPException(404, detail="rule not found")
    return {"ok": True}


@router.get("/classification/findings")
async def classification_findings(
    severity: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = await classification_service.list_findings(
        db, severity=severity, resource_type=resource_type,
        limit=limit, offset=offset,
    )
    return {
        "items": [
            {"id": r.id, "rule_id": r.rule_id,
             "resource_type": r.resource_type,
             "resource_id": r.resource_id,
             "field": r.field, "severity": r.severity,
             "confidence": r.confidence,
             "matched_text": r.matched_text, "evidence": r.evidence,
             "found_at": r.found_at.isoformat() if r.found_at else None}
            for r in rows
        ],
    }


@router.post("/classification/scan")
async def classification_scan(
    body: ClassificationScanIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    return await classification_service.scan(
        db, scope=body.scope, dry_run=body.dry_run, actor_id=user_id,
    )


# ── FRAMEWORKS ──────────────────────────────────────────────────


@router.get("/frameworks/status")
async def frameworks_status(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    return await framework_engine.status_all(db)


@router.get("/frameworks/{framework}")
async def framework_assess(
    framework: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    try:
        return await framework_engine.assess(db, framework)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


# ── REPORTS v2 ──────────────────────────────────────────────────


class ReportGenerateIn(BaseModel):
    period: int = Field(default=90, ge=1, le=3650)
    format: str = "json"
    signed: bool = True


@router.get("/reports")
async def reports_list(
    framework: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = await report_generator.list_reports(
        db, framework=framework, limit=limit, offset=offset,
    )
    return {
        "items": [
            {"id": r.id, "framework": r.framework, "format": r.format,
             "status": r.status,
             "period_start": r.period_start.isoformat() if r.period_start else None,
             "period_end": r.period_end.isoformat() if r.period_end else None,
             "sha256": r.sha256, "signed": r.signed,
             "size_bytes": r.size_bytes,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "summary": r.summary}
            for r in rows
        ],
    }


@router.post("/reports/{framework}")
async def reports_generate_v2(
    framework: str,
    body: ReportGenerateIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    if framework not in VALID_FRAMEWORKS:
        raise HTTPException(400, detail=f"framework must be one of {VALID_FRAMEWORKS}")
    if body.format not in VALID_REPORT_FORMATS:
        raise HTTPException(400, detail=f"format must be one of {VALID_REPORT_FORMATS}")
    row = await report_generator.generate(
        db, framework=framework, format=body.format,
        period_days=body.period, signed=body.signed, actor_id=user_id,
    )
    return {
        "report_job_id": row.id, "framework": row.framework,
        "status": row.status, "format": row.format,
        "sha256": row.sha256, "signed": row.signed,
        "size_bytes": row.size_bytes,
    }


@router.get("/reports/schedules")
async def reports_schedules(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(ComplianceReportSchedule)
    )).scalars().all()
    return {
        "items": [
            {"id": s.id, "framework": s.framework, "format": s.format,
             "cadence": s.cadence, "enabled": s.enabled,
             "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
             "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
             "last_report_id": s.last_report_id,
             "recipients": s.recipients}
            for s in rows
        ],
    }


# ── AUDIT LINKAGE ───────────────────────────────────────────────


@router.get("/audit")
async def compliance_audit_entries(
    source: str = Query("compliance"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    try:
        from app.models.audit_log import AuditLog
    except Exception:
        return {"items": []}
    q = select(AuditLog)
    if source:
        q = q.where(AuditLog.event.ilike(f"{source}%"))
    q = q.order_by(desc(getattr(AuditLog, "occurred_at", AuditLog.id)))
    q = q.offset(offset).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return {
        "items": [
            {"id": r.id, "event": r.event,
             "user_id": r.user_id, "ip_address": r.ip_address,
             "success": r.success,
             "occurred_at": (getattr(r, "occurred_at", None)
                             .isoformat() if getattr(r, "occurred_at", None) else None),
             "details": getattr(r, "details_json", None)}
            for r in rows
        ],
    }


@router.get("/audit/verify")
async def compliance_audit_verify(
    _user: str = Depends(require_permission(_PERM)),
):
    try:
        from app.services.audit.chain import head_info
        return head_info(verify=True)
    except Exception as e:
        return {"verify_status": "unknown", "error": str(e)}
