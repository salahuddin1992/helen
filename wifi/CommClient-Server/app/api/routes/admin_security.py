"""
Phase 6 / Module AE — Security admin REST endpoints.

Mounted under ``/api/admin/security``. Requires ``security.manage``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.security import (
    IPBlock, LoginAttempt, SecurityAdvisory, SecurityEvent,
)
from app.security.dependency_scanner import get_dep_scanner
from app.security.intrusion_detection import get_ids
from app.security.rate_limiter_v2 import get_rate_limiter
from app.security.secrets_rotation import (
    VALID_SECRET_KINDS, get_secrets_rotator,
)
from app.security.waf_middleware import get_waf
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin/security", tags=["admin-security"])
_PERM = "security.manage"


# ── shapes ──────────────────────────────────────────────────


class IPBlockIn(BaseModel):
    ip_cidr: str
    reason: str = ""
    duration_minutes: Optional[int] = None    # None = permanent


class IPBlockOut(BaseModel):
    id: str
    ip_cidr: str
    reason: str
    blocked_at: datetime
    expires_at: Optional[datetime]
    blocked_by: Optional[str]


class LoginAttemptOut(BaseModel):
    id: str
    username: str
    ip: str
    success: bool
    attempted_at: datetime
    user_agent: Optional[str]


class SecurityEventOut(BaseModel):
    id: str
    kind: str
    severity: str
    ip: Optional[str]
    user_id: Optional[str]
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AdvisoryOut(BaseModel):
    id: str
    package: str
    version: str
    cve: Optional[str]
    severity: str
    summary: str
    fixed_in: Optional[str]
    discovered_at: datetime
    acknowledged: bool
    acknowledged_at: Optional[datetime]
    acknowledged_by: Optional[str]


# ── IP blocks ───────────────────────────────────────────────


@router.get("/blocks", response_model=list[IPBlockOut])
async def list_blocks(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    res = await db.execute(
        select(IPBlock).order_by(desc(IPBlock.blocked_at)).limit(500)
    )
    return [IPBlockOut(
        id=r.id, ip_cidr=r.ip_cidr, reason=r.reason,
        blocked_at=r.blocked_at, expires_at=r.expires_at,
        blocked_by=r.blocked_by,
    ) for r in res.scalars().all()]


@router.post("/blocks", response_model=IPBlockOut)
async def create_block(
    body: IPBlockIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    expires = None
    if body.duration_minutes is not None and body.duration_minutes > 0:
        expires = datetime.now(timezone.utc) + timedelta(minutes=body.duration_minutes)
    row = IPBlock(
        ip_cidr=body.ip_cidr, reason=body.reason,
        expires_at=expires, blocked_by=user_id,
    )
    db.add(row)
    db.add(SecurityEvent(
        kind="ip_blocked", severity="warning",
        ip=body.ip_cidr, user_id=user_id,
        payload={"manual": True, "reason": body.reason,
                 "duration_minutes": body.duration_minutes},
    ))
    await db.commit()
    await db.refresh(row)
    return IPBlockOut(
        id=row.id, ip_cidr=row.ip_cidr, reason=row.reason,
        blocked_at=row.blocked_at, expires_at=row.expires_at,
        blocked_by=row.blocked_by,
    )


@router.delete("/blocks/{block_id}")
async def delete_block(
    block_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(IPBlock).where(IPBlock.id == block_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "block not found")
    ip = row.ip_cidr
    await db.delete(row)
    db.add(SecurityEvent(
        kind="ip_unblocked", severity="info",
        ip=ip, user_id=user_id, payload={"manual": True},
    ))
    await db.commit()
    return {"status": "unblocked"}


# ── security events / login attempts ────────────────────────


@router.get("/events", response_model=list[SecurityEventOut])
async def list_events(
    severity: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    limit: int = Query(default=100, le=1000),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = select(SecurityEvent).order_by(desc(SecurityEvent.created_at)).limit(limit)
    if severity:
        q = q.where(SecurityEvent.severity == severity)
    if kind:
        q = q.where(SecurityEvent.kind == kind)
    res = await db.execute(q)
    return [SecurityEventOut(
        id=r.id, kind=r.kind, severity=r.severity, ip=r.ip,
        user_id=r.user_id, payload=r.payload or {}, created_at=r.created_at,
    ) for r in res.scalars().all()]


@router.get("/login-attempts", response_model=list[LoginAttemptOut])
async def list_login_attempts(
    username: Optional[str] = Query(default=None),
    ip: Optional[str] = Query(default=None),
    success: Optional[bool] = Query(default=None),
    limit: int = Query(default=100, le=1000),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = select(LoginAttempt).order_by(desc(LoginAttempt.attempted_at)).limit(limit)
    if username:
        q = q.where(LoginAttempt.username == username)
    if ip:
        q = q.where(LoginAttempt.ip == ip)
    if success is not None:
        q = q.where(LoginAttempt.success == bool(success))
    res = await db.execute(q)
    return [LoginAttemptOut(
        id=r.id, username=r.username, ip=r.ip, success=r.success,
        attempted_at=r.attempted_at, user_agent=r.user_agent,
    ) for r in res.scalars().all()]


# ── advisories ──────────────────────────────────────────────


@router.get("/advisories", response_model=list[AdvisoryOut])
async def list_advisories(
    acknowledged: Optional[bool] = Query(default=None),
    limit: int = Query(default=200, le=2000),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = select(SecurityAdvisory).order_by(desc(SecurityAdvisory.discovered_at)).limit(limit)
    if acknowledged is not None:
        q = q.where(SecurityAdvisory.acknowledged == bool(acknowledged))
    res = await db.execute(q)
    return [AdvisoryOut(
        id=r.id, package=r.package, version=r.version, cve=r.cve,
        severity=r.severity, summary=r.summary, fixed_in=r.fixed_in,
        discovered_at=r.discovered_at, acknowledged=r.acknowledged,
        acknowledged_at=r.acknowledged_at, acknowledged_by=r.acknowledged_by,
    ) for r in res.scalars().all()]


@router.post("/advisories/{advisory_id}/acknowledge")
async def acknowledge_advisory(
    advisory_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(SecurityAdvisory).where(SecurityAdvisory.id == advisory_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "advisory not found")
    row.acknowledged = True
    row.acknowledged_at = datetime.now(timezone.utc)
    row.acknowledged_by = user_id
    await db.commit()
    return {"status": "acknowledged"}


@router.post("/advisories/scan-now")
async def scan_advisories_now(
    _user: str = Depends(require_permission(_PERM)),
):
    n = await get_dep_scanner().scan_now()
    return {"new_advisories": n}


# ── WAF / IDS / rate-limit stats ────────────────────────────


@router.get("/waf/stats")
async def waf_stats(
    _user: str = Depends(require_permission(_PERM)),
):
    waf = get_waf()
    if waf is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "modes": dict(waf.cfg.modes),
        "stats": waf.stats(),
    }


@router.post("/waf/reset-stats")
async def waf_reset_stats(
    _user: str = Depends(require_permission(_PERM)),
):
    waf = get_waf()
    if waf is None:
        raise HTTPException(503, "waf not enabled")
    waf.reset_stats()
    return {"status": "ok"}


@router.get("/rate-limit/config")
async def rate_limit_config(
    _user: str = Depends(require_permission(_PERM)),
):
    rl = get_rate_limiter()
    if rl is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "per_ip": rl.cfg.per_ip.__dict__,
        "per_user": rl.cfg.per_user.__dict__,
        "per_route": {k: v.__dict__ for k, v in rl.cfg.per_route.items()},
        "cidr_whitelist": rl.cfg.cidr_whitelist,
    }


# ── secret rotation ─────────────────────────────────────────


@router.get("/rotate/policies")
async def list_rotate_policies(
    _user: str = Depends(require_permission(_PERM)),
):
    return get_secrets_rotator().list_policies()


@router.post("/rotate/{secret_kind}")
async def rotate_secret(
    secret_kind: str = Path(...),
    user_id: str = Depends(require_permission(_PERM)),
):
    if secret_kind not in VALID_SECRET_KINDS:
        raise HTTPException(400, f"invalid kind. allowed: {VALID_SECRET_KINDS}")
    result = await get_secrets_rotator().rotate(secret_kind, manual_by=user_id)
    return result
