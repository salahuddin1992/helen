"""
Zero-Trust admin REST endpoints. Requires ``zt.admin``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.zt import (
    AccessPolicy, AccessRequest, DeviceAttestation,
    JITGrant, WorkloadIdentity,
)
from app.services.rbac.enforcer import require_permission
from app.services.zt.jit_access import get_jit_access
from app.services.zt.policy_engine import (
    DecisionContext, get_policy_engine,
)
from app.services.zt.risk_engine import get_risk_engine
from app.services.zt.spiffe_authority import get_spiffe_authority

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin/zt", tags=["admin-zt"])
_PERM = "zt.admin"


# ── shapes ──────────────────────────────────────────────────


class IdentityIn(BaseModel):
    name: str
    workload_type: str = "service"
    attributes: dict[str, Any] = {}
    ttl_hours: int = 1


class PolicyIn(BaseModel):
    name: str
    subject_selector: dict[str, Any] = {}
    resource_selector: dict[str, Any] = {}
    action: str
    allow: bool = True
    conditions: dict[str, Any] = {}
    obligations: dict[str, Any] = {}
    priority: int = 100
    enabled: bool = True
    description: str = ""


class PolicyTestIn(BaseModel):
    identity: str
    role: str = ""
    workload_kind: str = "user"
    ip: str = ""
    country: str = ""
    risk_score: int = 0
    device_attested: bool = False
    mfa_passed: bool = False
    resource: str
    action: str


# ── identities ──────────────────────────────────────────────


@router.get("/identities")
async def list_identities(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(WorkloadIdentity).order_by(desc(WorkloadIdentity.created_at))
    )).scalars().all()
    return [
        {
            "id":             r.id,
            "spiffe_id":      r.spiffe_id,
            "workload_type":  r.workload_type,
            "issued_at":      r.issued_at,
            "expires_at":     r.expires_at,
            "revoked":        r.revoked,
            "attributes":     r.attributes,
        }
        for r in rows
    ]


@router.post("/identities")
async def issue_identity(
    payload: IdentityIn,
    _u: str = Depends(require_permission(_PERM)),
):
    svid = await get_spiffe_authority().issue(
        payload.workload_type, payload.name,
        attributes=payload.attributes,
        ttl=timedelta(hours=payload.ttl_hours),
    )
    return {
        "spiffe_id":  svid.spiffe_id,
        "jwt":        svid.jwt,
        "expires_at": svid.expires_at,
    }


@router.delete("/identities/{id}")
async def revoke_identity(
    id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(WorkloadIdentity).where(WorkloadIdentity.id == id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    await get_spiffe_authority().revoke(row.spiffe_id)
    return {"ok": True}


# ── policies ────────────────────────────────────────────────


@router.get("/policies")
async def list_policies(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(AccessPolicy).order_by(AccessPolicy.priority)
    )).scalars().all()
    return [
        {
            "id":               r.id,
            "name":             r.name,
            "subject_selector": r.subject_selector,
            "resource_selector": r.resource_selector,
            "action":           r.action,
            "allow":            r.allow,
            "conditions":       r.conditions,
            "obligations":      r.obligations,
            "priority":         r.priority,
            "enabled":          r.enabled,
            "description":      r.description,
        }
        for r in rows
    ]


@router.post("/policies")
async def create_policy(
    payload: PolicyIn,
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    row = AccessPolicy(**payload.dict())
    db.add(row)
    await db.commit()
    get_policy_engine().invalidate_cache()
    return {"ok": True, "id": row.id}


@router.put("/policies/{id}")
async def update_policy(
    id: str,
    payload: PolicyIn,
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(AccessPolicy).where(AccessPolicy.id == id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    for k, v in payload.dict().items():
        setattr(row, k, v)
    await db.commit()
    get_policy_engine().invalidate_cache()
    return {"ok": True}


@router.delete("/policies/{id}")
async def delete_policy(
    id: str,
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(AccessPolicy).where(AccessPolicy.id == id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    await db.delete(row)
    await db.commit()
    get_policy_engine().invalidate_cache()
    return {"ok": True}


@router.post("/policies/test")
async def test_policy(
    payload: PolicyTestIn,
    _u: str = Depends(require_permission(_PERM)),
):
    ctx = DecisionContext(
        identity=payload.identity,
        workload_kind=payload.workload_kind,
        role=payload.role,
        ip=payload.ip,
        country=payload.country,
        risk_score=payload.risk_score,
        device_attested=payload.device_attested,
        mfa_passed=payload.mfa_passed,
    )
    decision = await get_policy_engine().evaluate(
        ctx=ctx, resource=payload.resource, action=payload.action,
        persist=False,
    )
    return {
        "allow":          decision.allow,
        "reasons":        decision.reasons,
        "obligations":    decision.obligations,
        "matched_policy": decision.matched_policy,
    }


# ── access log ──────────────────────────────────────────────


@router.get("/access-log")
async def access_log(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
    limit: int = Query(200, ge=1, le=2000),
    decision: Optional[str] = None,
):
    q = select(AccessRequest).order_by(desc(AccessRequest.decided_at))
    if decision:
        q = q.where(AccessRequest.decision == decision)
    q = q.limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id":          r.id,
            "subject":     r.requester_identity,
            "resource":    r.resource,
            "action":      r.action,
            "decision":    r.decision,
            "reasons":     r.reasons,
            "obligations": r.obligations,
            "session_id":  r.session_id,
            "risk_score":  r.risk_score,
            "decided_at":  r.decided_at,
        }
        for r in rows
    ]


# ── device attestations ─────────────────────────────────────


@router.get("/device-attestations")
async def device_attestations(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
    limit: int = Query(500, ge=1, le=2000),
):
    rows = (await db.execute(
        select(DeviceAttestation)
        .order_by(desc(DeviceAttestation.attested_at))
        .limit(limit)
    )).scalars().all()
    return [
        {
            "id":           r.id,
            "device_id":    r.device_id,
            "user_id":      r.user_id,
            "os":           r.os,
            "os_version":   r.os_version,
            "app_version":  r.app_version,
            "disk_encrypted": r.disk_encrypted,
            "screen_lock":  r.screen_lock,
            "antivirus_active": r.antivirus_active,
            "jailbroken":   r.jailbroken,
            "risk_score":   r.risk_score,
            "attested_at":  r.attested_at,
            "valid_until":  r.valid_until,
        }
        for r in rows
    ]


# ── JIT ─────────────────────────────────────────────────────


@router.get("/jit-grants")
async def jit_grants(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(JITGrant).order_by(desc(JITGrant.created_at)).limit(500)
    )).scalars().all()
    return [
        {
            "id":         r.id,
            "user_id":    r.user_id,
            "resource":   r.resource,
            "scopes":     r.scopes,
            "reason":     r.reason,
            "granted_by": r.granted_by,
            "granted_at": r.granted_at,
            "expires_at": r.expires_at,
            "revoked_at": r.revoked_at,
            "status":     r.status,
        }
        for r in rows
    ]


@router.post("/jit-grants/{id}/approve")
async def jit_approve(
    id: str,
    _u: str = Depends(require_permission(_PERM)),
):
    row = await get_jit_access().approve(id, _u)
    if row is None:
        raise HTTPException(status_code=400, detail="cannot_approve")
    return {"ok": True, "status": row.status}


@router.post("/jit-grants/{id}/revoke")
async def jit_revoke(
    id: str,
    _u: str = Depends(require_permission(_PERM)),
):
    ok = await get_jit_access().revoke(id)
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


# ── risk dashboard ──────────────────────────────────────────


@router.get("/risk-events")
async def risk_events(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
    limit: int = Query(200, ge=1, le=1000),
):
    rows = (await db.execute(
        select(AccessRequest)
        .where(AccessRequest.risk_score >= 30)
        .order_by(desc(AccessRequest.decided_at))
        .limit(limit)
    )).scalars().all()
    return [
        {
            "id":          r.id,
            "subject":     r.requester_identity,
            "resource":    r.resource,
            "action":      r.action,
            "decision":    r.decision,
            "risk_score":  r.risk_score,
            "decided_at":  r.decided_at,
        }
        for r in rows
    ]
