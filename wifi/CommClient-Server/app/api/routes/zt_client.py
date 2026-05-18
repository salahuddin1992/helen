"""
Zero-Trust client-facing endpoints. Used by user agents and edge
workers to attest, fetch their own identity, and request JIT access.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.zt import JITGrant
from app.services.zt.device_posture import get_device_posture
from app.services.zt.jit_access import get_jit_access
from app.services.zt.policy_engine import (
    DecisionContext, get_policy_engine,
)
from app.services.zt.spiffe_authority import (
    get_spiffe_authority, verify_jwt,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/zt", tags=["zt"])


class AttestIn(BaseModel):
    device_id: str
    os: str
    os_version: str
    app_version: str = ""
    disk_encrypted: bool = False
    screen_lock: bool = False
    antivirus_active: bool = False
    jailbroken: bool = False


@router.post("/attest")
async def submit_attestation(
    payload: AttestIn,
    request: Request,
):
    identity = _extract_identity(request)
    user_id = None
    if identity:
        sub = identity.get("sub") or ""
        if sub.startswith("spiffe://"):
            user_id = sub.rsplit("/", 1)[-1]
    row = await get_device_posture().submit(
        device_id=payload.device_id,
        user_id=user_id,
        os=payload.os,
        os_version=payload.os_version,
        app_version=payload.app_version,
        disk_encrypted=payload.disk_encrypted,
        screen_lock=payload.screen_lock,
        antivirus_active=payload.antivirus_active,
        jailbroken=payload.jailbroken,
    )
    return {
        "ok":           True,
        "device_id":    row.device_id,
        "risk_score":   row.risk_score,
        "valid_until":  row.valid_until,
    }


@router.get("/identity/me")
async def my_identity(request: Request):
    identity = _extract_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="no_identity")
    return {
        "spiffe_id":  identity.get("sub"),
        "workload":   identity.get("workload"),
        "exp":        identity.get("exp"),
        "audience":   identity.get("aud"),
    }


@router.get("/trust-bundle")
async def trust_bundle():
    return await get_spiffe_authority().trust_bundle()


class JITRequestIn(BaseModel):
    resource: str
    scopes: list[str]
    reason: str
    ttl_hours: int = 1


@router.post("/jit-request")
async def request_jit(
    payload: JITRequestIn,
    request: Request,
):
    identity = _extract_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="no_identity")
    sub = identity.get("sub") or ""
    user_id = sub.rsplit("/", 1)[-1] if sub.startswith("spiffe://") else sub
    grant = await get_jit_access().request_grant(
        user_id=user_id,
        resource=payload.resource,
        scopes=payload.scopes,
        reason=payload.reason,
        ttl_hours=payload.ttl_hours,
    )
    return {
        "id":          grant.id,
        "status":      grant.status,
        "expires_at":  grant.expires_at,
    }


@router.get("/jit-request/{id}/status")
async def jit_status(id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(JITGrant).where(JITGrant.id == id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return {
        "id":         row.id,
        "status":     row.status,
        "granted_at": row.granted_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
    }


class PolicyTestQuery(BaseModel):
    resource: str
    action: str


@router.get("/policy/test")
async def policy_test(
    request: Request,
    resource: str = Query(...),
    action: str = Query(...),
):
    identity = _extract_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="no_identity")
    ctx = DecisionContext(
        identity=identity.get("sub") or "",
        workload_kind=identity.get("workload") or "user",
        role=identity.get("role") or "",
        ip=(request.client.host if request.client else ""),
    )
    decision = await get_policy_engine().evaluate(
        ctx=ctx, resource=resource, action=action, persist=False,
    )
    return {
        "allow":       decision.allow,
        "reasons":     decision.reasons,
        "obligations": decision.obligations,
    }


# ── helpers ─────────────────────────────────────────────────


def _extract_identity(request: Request) -> Optional[dict[str, Any]]:
    auth = request.headers.get("authorization") or ""
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.headers.get("x-zt-svid") or ""
    if not token:
        token = request.cookies.get("zt_svid") or ""
    if not token:
        return None
    return verify_jwt(token)
