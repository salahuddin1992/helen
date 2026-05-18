"""
Tenancy + RBAC + Billing Portal — production admin router.

This module mounts under ``/api/admin`` and exposes the full surface area
the Helen Operator Console requires for self-service tenancy and billing
management. It complements (and never overwrites) the existing
``admin_billing`` router by adding:

    /api/admin/tenants/...
    /api/admin/workspaces/...
    /api/admin/rbac/...
    /api/admin/billing/plans/...           (with audit history)
    /api/admin/billing/licenses/...        (Ed25519-signed offline keys)
    /api/admin/billing/usage/...
    /api/admin/billing/invoices/...        (generate/regenerate/email/pdf)

All routes require ``billing.manage`` OR ``rbac.roles_write`` permissions
depending on the resource. Destructive operations append to the
tamper-evident :mod:`app.services.audit_chain` and write a standard
:func:`audit_log` row.

Style notes:
* Endpoints stay thin — heavy lifting lives in service modules.
* Every list endpoint paginates (``page`` / ``page_size``).
* Every mutator is idempotent where reasonable (upsert by slug/code/key).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.base import utc_now
from app.models.billing import (
    Invoice,
    Plan,
    Subscription,
    UsageRecord,
)
from app.models.billing_license import (
    BillingLicense,
    LicenseRevocation,
    PlanAuditEntry,
    RbacPasswordReset,
    TenantAdminSession,
)
from app.models.channel import Channel
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.session import UserSession
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services.audit_chain import get_audit_chain
from app.services.billing.license_signer import (
    build_license_payload,
    get_signer,
    sha256_hex,
)
from app.services.billing.portal_invoices import InvoiceGenerator
from app.services.billing.usage_meter import UsageMeter
from app.services.rbac.enforcer import (
    invalidate as rbac_invalidate,
    require_permission,
)
from app.services.rbac.registry import all_permission_keys
from app.services.tenancy import workspace_service

logger = get_logger(__name__)


router = APIRouter(prefix="/api/admin", tags=["admin-tenancy-portal"])


# Permission gates used throughout this module ---------------------------
_PERM_BILLING = "billing.manage"
_PERM_RBAC = "rbac.roles_write"
_PERM_USERS = "users.promote"


def _ok(d: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, **(d or {})}


def _chain_append(actor: str, action: str, target: str, payload: dict[str, Any]) -> None:
    """Best-effort tamper-evident audit append (silently swallowed on
    failure — never break the request flow)."""
    try:
        get_audit_chain().append(
            actor=actor, action=action, target=target, payload=payload,
        )
    except Exception as e:                                             # noqa: BLE001
        logger.warning("audit-chain append failed action=%s err=%s", action, e)


# ═══════════════════════════════════════════════════════════════════════
# TENANTS
# ═══════════════════════════════════════════════════════════════════════


class TenantIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    owner_id: str
    plan: str = "free"
    description: Optional[str] = None
    slug: Optional[str] = None


class TenantPatch(BaseModel):
    name: Optional[str] = None
    plan: Optional[str] = None
    status: Optional[str] = Field(None, pattern="^(active|suspended|archived)$")
    description: Optional[str] = None
    settings: Optional[dict[str, Any]] = None


class QuotaIn(BaseModel):
    quotas: dict[str, Any] = Field(..., description="metric → limit")


def _tenant_status(ws: Workspace) -> str:
    """Synthesise the status string from existing fields."""
    s = (ws.settings or {}).get("status")
    if s:
        return s
    if not ws.is_active:
        return "archived"
    return "active"


def _tenant_dict(ws: Workspace) -> dict[str, Any]:
    return {
        "id": ws.id,
        "slug": ws.slug,
        "name": ws.name,
        "description": ws.description,
        "owner_id": ws.owner_id,
        "plan": ws.plan,
        "status": _tenant_status(ws),
        "is_active": ws.is_active,
        "settings": ws.settings or {},
        "created_at": ws.created_at.isoformat() if ws.created_at else None,
        "updated_at": ws.updated_at.isoformat() if ws.updated_at else None,
    }


@router.get("/tenants")
async def list_tenants(
    status_: Optional[str] = Query(None, alias="status"),
    plan: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    stmt = select(Workspace)
    if plan:
        stmt = stmt.where(Workspace.plan == plan)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(
            Workspace.name.ilike(like),
            Workspace.slug.ilike(like),
            Workspace.description.ilike(like),
        ))
    if status_ == "archived":
        stmt = stmt.where(Workspace.is_active.is_(False))
    elif status_ == "active":
        stmt = stmt.where(Workspace.is_active.is_(True))

    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one()
    rows = (await db.execute(
        stmt.order_by(desc(Workspace.created_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    items = [_tenant_dict(w) for w in rows]
    if status_ in (None, "active"):
        # post-filter by settings.status when caller asks for non-binary
        # statuses (suspended)
        if status_ and status_ != "active":
            items = [i for i in items if i["status"] == status_]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/tenants", status_code=201)
async def create_tenant(
    body: TenantIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    if not await db.get(User, body.owner_id):
        raise HTTPException(404, detail="owner-not-found")
    try:
        ws = await workspace_service.create_workspace(
            db, owner=body.owner_id, name=body.name,
            slug=body.slug, description=body.description, plan=body.plan,
        )
        await db.commit()
    except workspace_service.WorkspaceError as e:
        raise HTTPException(400, detail=str(e))

    audit_log("admin.tenant.created", user_id=user_id, success=True,
              details={"tenant_id": ws.id, "owner": body.owner_id, "plan": body.plan})
    _chain_append(user_id, "tenant.created", ws.id, {"plan": body.plan, "name": body.name})
    return _tenant_dict(ws)


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")
    return _tenant_dict(ws)


@router.put("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    body: TenantPatch,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")
    before = _tenant_dict(ws)

    if body.name is not None:
        ws.name = body.name.strip() or ws.name
    if body.description is not None:
        ws.description = body.description
    if body.plan is not None:
        ws.plan = body.plan
    if body.status is not None:
        s = dict(ws.settings or {})
        s["status"] = body.status
        ws.settings = s
        if body.status == "archived":
            ws.is_active = False
        else:
            ws.is_active = True
    if body.settings is not None:
        s = dict(ws.settings or {})
        s.update(body.settings)
        ws.settings = s

    await db.commit()
    audit_log("admin.tenant.updated", user_id=user_id, success=True,
              details={"tenant_id": ws.id, "before": before, "after": _tenant_dict(ws)})
    _chain_append(user_id, "tenant.updated", ws.id,
                  {"plan": ws.plan, "status": _tenant_status(ws)})
    return _tenant_dict(ws)


@router.delete("/tenants/{tenant_id}")
async def soft_delete_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")
    s = dict(ws.settings or {})
    s["status"] = "archived"
    ws.settings = s
    ws.is_active = False
    await db.commit()
    audit_log("admin.tenant.archived", user_id=user_id, success=True,
              details={"tenant_id": tenant_id})
    _chain_append(user_id, "tenant.archived", tenant_id, {})
    return _ok({"tenant_id": tenant_id, "status": "archived"})


@router.post("/tenants/{tenant_id}/suspend")
async def suspend_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")
    s = dict(ws.settings or {})
    s["status"] = "suspended"
    ws.settings = s
    ws.is_active = False
    await db.commit()
    audit_log("admin.tenant.suspended", user_id=user_id, success=True,
              details={"tenant_id": tenant_id})
    _chain_append(user_id, "tenant.suspended", tenant_id, {})
    return _ok({"tenant_id": tenant_id, "status": "suspended"})


@router.post("/tenants/{tenant_id}/resume")
async def resume_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")
    s = dict(ws.settings or {})
    s["status"] = "active"
    ws.settings = s
    ws.is_active = True
    await db.commit()
    audit_log("admin.tenant.resumed", user_id=user_id, success=True,
              details={"tenant_id": tenant_id})
    _chain_append(user_id, "tenant.resumed", tenant_id, {})
    return _ok({"tenant_id": tenant_id, "status": "active"})


@router.post("/tenants/{tenant_id}/impersonate")
async def impersonate_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")
    sess = TenantAdminSession(
        workspace_id=tenant_id,
        issued_by=user_id,
        note=f"impersonation by {user_id}",
    )
    db.add(sess)
    await db.commit()
    audit_log("admin.tenant.impersonate", user_id=user_id, success=True,
              details={"tenant_id": tenant_id, "session_id": sess.id})
    _chain_append(user_id, "tenant.impersonate", tenant_id, {"session_id": sess.id})
    return {
        "token": sess.token,
        "workspace_id": tenant_id,
        "expires_at": sess.expires_at.isoformat(),
        "ttl_seconds": int((sess.expires_at - utc_now()).total_seconds()),
    }


@router.get("/tenants/{tenant_id}/stats")
async def tenant_stats(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")

    users_count = (await db.execute(
        select(func.count(WorkspaceMember.id))
        .where(WorkspaceMember.workspace_id == tenant_id)
    )).scalar_one()
    channels_count = (await db.execute(
        select(func.count(Channel.id)).where(
            getattr(Channel, "workspace_id", None) == tenant_id
        ) if hasattr(Channel, "workspace_id") else select(func.count(Channel.id))
    )).scalar_one()
    storage_gb = (await db.execute(
        select(func.coalesce(func.sum(UsageRecord.value), 0))
        .where(and_(
            UsageRecord.workspace_id == tenant_id,
            UsageRecord.metric == "storage_gb",
        ))
    )).scalar_one()
    calls = (await db.execute(
        select(func.coalesce(func.sum(UsageRecord.value), 0))
        .where(and_(
            UsageRecord.workspace_id == tenant_id,
            UsageRecord.metric == "agent_minutes",
        ))
    )).scalar_one()
    return {
        "tenant_id": tenant_id,
        "users": int(users_count or 0),
        "workspaces": 1,
        "channels": int(channels_count or 0),
        "storage_gb": float(storage_gb or 0),
        "call_minutes": float(calls or 0),
    }


@router.get("/tenants/{tenant_id}/quota")
async def get_tenant_quota(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")
    settings = ws.settings or {}
    override = settings.get("quota_overrides") or {}
    plan_row = (await db.execute(
        select(Plan).where(Plan.slug == ws.plan)
    )).scalar_one_or_none()
    base = dict(plan_row.included_quotas or {}) if plan_row else {}
    merged = {**base, **override}
    return {"tenant_id": tenant_id, "plan": ws.plan, "quotas": merged, "overrides": override}


@router.put("/tenants/{tenant_id}/quota")
async def set_tenant_quota(
    tenant_id: str,
    body: QuotaIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")
    s = dict(ws.settings or {})
    s["quota_overrides"] = dict(body.quotas or {})
    ws.settings = s
    await db.commit()
    audit_log("admin.tenant.quota_updated", user_id=user_id, success=True,
              details={"tenant_id": tenant_id, "quotas": body.quotas})
    _chain_append(user_id, "tenant.quota_updated", tenant_id, {"quotas": body.quotas})
    return _ok({"tenant_id": tenant_id, "quotas": body.quotas})


@router.get("/tenants/{tenant_id}/usage")
async def tenant_usage(
    tenant_id: str,
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = None,
    endpoint: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    return await UsageMeter.get_history(
        db, tenant_id, from_dt=from_, to_dt=to, endpoint=endpoint,
    )


@router.get("/tenants/{tenant_id}/members")
async def tenant_members(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    rows = (await db.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == tenant_id)
        .order_by(WorkspaceMember.joined_at.asc())
    )).all()
    return {
        "tenant_id": tenant_id,
        "members": [
            {
                "user_id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "role": m.role,
                "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                "invited_by": m.invited_by,
            }
            for m, u in rows
        ],
    }


@router.get("/tenants/{tenant_id}/export")
async def export_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")

    # Build a zip in-memory with one JSON document per resource type.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("workspace.json", json.dumps(
            _tenant_dict(ws), ensure_ascii=False, indent=2,
        ))
        members = (await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == tenant_id
            )
        )).scalars().all()
        z.writestr("members.json", json.dumps([
            {"user_id": m.user_id, "role": m.role,
             "joined_at": m.joined_at.isoformat() if m.joined_at else None}
            for m in members
        ], ensure_ascii=False, indent=2))
        subs = (await db.execute(
            select(Subscription).where(Subscription.workspace_id == tenant_id)
        )).scalars().all()
        z.writestr("subscriptions.json", json.dumps([
            {"id": s.id, "plan_id": s.plan_id, "status": s.status,
             "billing_cycle": s.billing_cycle,
             "current_period_end": s.current_period_end.isoformat()
             if s.current_period_end else None}
            for s in subs
        ], ensure_ascii=False, indent=2))
        invoices = (await db.execute(
            select(Invoice).where(Invoice.workspace_id == tenant_id)
        )).scalars().all()
        z.writestr("invoices.json", json.dumps([
            {"id": i.id, "number": i.number, "status": i.status,
             "total_cents": i.total_cents, "currency": i.currency,
             "period_start": i.period_start.isoformat() if i.period_start else None,
             "period_end": i.period_end.isoformat() if i.period_end else None}
            for i in invoices
        ], ensure_ascii=False, indent=2))
        usage = (await db.execute(
            select(UsageRecord).where(UsageRecord.workspace_id == tenant_id)
        )).scalars().all()
        z.writestr("usage.json", json.dumps([
            {"metric": r.metric, "value": float(r.value or 0),
             "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None}
            for r in usage
        ], ensure_ascii=False, indent=2))
    buf.seek(0)
    audit_log("admin.tenant.exported", user_id=user_id, success=True,
              details={"tenant_id": tenant_id})
    _chain_append(user_id, "tenant.exported", tenant_id, {})
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="tenant-{tenant_id}.zip"',
        },
    )


@router.post("/tenants/{tenant_id}/rotate-secrets")
async def rotate_tenant_secrets(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")
    s = dict(ws.settings or {})
    new_secret = hashlib.sha256(os.urandom(48)).hexdigest()
    s["api_secret"] = new_secret
    s["api_secret_rotated_at"] = utc_now().isoformat()
    ws.settings = s
    await db.commit()
    audit_log("admin.tenant.secrets_rotated", user_id=user_id, success=True,
              details={"tenant_id": tenant_id})
    _chain_append(user_id, "tenant.secrets_rotated", tenant_id, {})
    return _ok({"tenant_id": tenant_id, "api_secret": new_secret})


# ═══════════════════════════════════════════════════════════════════════
# WORKSPACES
# ═══════════════════════════════════════════════════════════════════════


class WorkspaceIn(BaseModel):
    tenant_id: str
    name: str
    owner_id: str
    description: Optional[str] = None
    plan: str = "free"


class WorkspacePatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    plan: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("/workspaces")
async def list_workspaces(
    tenant_id: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    stmt = select(Workspace)
    if tenant_id:
        stmt = stmt.where(Workspace.id == tenant_id)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(Workspace.name.ilike(like), Workspace.slug.ilike(like)))
    rows = (await db.execute(
        stmt.order_by(desc(Workspace.created_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {"items": [_tenant_dict(w) for w in rows]}


@router.post("/workspaces", status_code=201)
async def create_workspace(
    body: WorkspaceIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    if not await db.get(User, body.owner_id):
        raise HTTPException(404, "owner-not-found")
    try:
        ws = await workspace_service.create_workspace(
            db, owner=body.owner_id, name=body.name,
            description=body.description, plan=body.plan,
        )
        await db.commit()
    except workspace_service.WorkspaceError as e:
        raise HTTPException(400, detail=str(e))
    audit_log("admin.workspace.created", user_id=user_id, success=True,
              details={"workspace_id": ws.id})
    return _tenant_dict(ws)


@router.put("/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    body: WorkspacePatch,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(404, "workspace-not-found")
    if body.name is not None:
        ws.name = body.name.strip() or ws.name
    if body.description is not None:
        ws.description = body.description
    if body.plan is not None:
        ws.plan = body.plan
    if body.is_active is not None:
        ws.is_active = body.is_active
    await db.commit()
    audit_log("admin.workspace.updated", user_id=user_id, success=True,
              details={"workspace_id": workspace_id})
    return _tenant_dict(ws)


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(404, "workspace-not-found")
    await db.delete(ws)
    await db.commit()
    audit_log("admin.workspace.deleted", user_id=user_id, success=True,
              details={"workspace_id": workspace_id})
    _chain_append(user_id, "workspace.deleted", workspace_id, {})
    return _ok({"workspace_id": workspace_id})


@router.get("/workspaces/{workspace_id}/members")
async def list_workspace_members(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    rows = (await db.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.joined_at.asc())
    )).all()
    return {
        "tenant_id": workspace_id,
        "members": [
            {
                "user_id": u.id, "username": u.username,
                "display_name": u.display_name, "role": m.role,
                "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                "invited_by": m.invited_by,
            }
            for m, u in rows
        ],
    }


@router.post("/workspaces/{workspace_id}/members/{user_id}")
async def add_workspace_member(
    workspace_id: str,
    user_id: str,
    role: str = Query("member"),
    db: AsyncSession = Depends(get_db),
    actor_id: str = Depends(require_permission(_PERM_BILLING)),
):
    if not await db.get(Workspace, workspace_id):
        raise HTTPException(404, "workspace-not-found")
    if not await db.get(User, user_id):
        raise HTTPException(404, "user-not-found")
    try:
        m = await workspace_service.add_member(
            db, workspace_id, user_id, role=role, invited_by=actor_id,
        )
        await db.commit()
    except workspace_service.WorkspaceError as e:
        raise HTTPException(400, detail=str(e))
    audit_log("admin.workspace.member_added", user_id=actor_id, success=True,
              details={"workspace_id": workspace_id, "user_id": user_id, "role": role})
    return {"id": m.id, "role": m.role}


@router.delete("/workspaces/{workspace_id}/members/{user_id}")
async def remove_workspace_member(
    workspace_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    actor_id: str = Depends(require_permission(_PERM_BILLING)),
):
    removed = await workspace_service.remove_member(db, workspace_id, user_id)
    await db.commit()
    audit_log("admin.workspace.member_removed", user_id=actor_id,
              success=bool(removed),
              details={"workspace_id": workspace_id, "user_id": user_id})
    return _ok({"removed": removed})


@router.get("/workspaces/{workspace_id}/channels")
async def list_workspace_channels(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    if hasattr(Channel, "workspace_id"):
        rows = (await db.execute(
            select(Channel).where(getattr(Channel, "workspace_id") == workspace_id)
        )).scalars().all()
    else:
        rows = []
    return {
        "workspace_id": workspace_id,
        "channels": [
            {"id": c.id, "name": getattr(c, "name", None),
             "type": getattr(c, "type", None)}
            for c in rows
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
# RBAC — Roles
# ═══════════════════════════════════════════════════════════════════════


class RoleIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: Optional[str] = None
    is_system: bool = False


class RolePatch(BaseModel):
    description: Optional[str] = None


class CloneIn(BaseModel):
    new_name: str = Field(..., min_length=1, max_length=64)


class PermsIn(BaseModel):
    permissions: list[str]


@router.get("/rbac/roles")
async def list_roles(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_RBAC)),
):
    rows = (await db.execute(select(Role).order_by(Role.name))).scalars().all()
    out = []
    for r in rows:
        perms = (await db.execute(
            select(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == r.id)
        )).scalars().all()
        out.append({
            "id": r.id, "name": r.name, "description": r.description,
            "is_system": r.is_system,
            "permissions": list(perms),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"items": out}


@router.post("/rbac/roles", status_code=201)
async def create_role(
    body: RoleIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_RBAC)),
):
    existing = (await db.execute(
        select(Role).where(Role.name == body.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "role-exists")
    r = Role(name=body.name, description=body.description, is_system=body.is_system)
    db.add(r)
    await db.commit()
    audit_log("admin.rbac.role_created", user_id=user_id, success=True,
              details={"role": body.name})
    _chain_append(user_id, "rbac.role_created", body.name, {})
    return {"id": r.id, "name": r.name}


@router.put("/rbac/roles/{name}")
async def update_role(
    name: str,
    body: RolePatch,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_RBAC)),
):
    r = (await db.execute(
        select(Role).where(Role.name == name)
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "role-not-found")
    if body.description is not None:
        r.description = body.description
    await db.commit()
    audit_log("admin.rbac.role_updated", user_id=user_id, success=True,
              details={"role": name})
    return _ok({"role": name})


@router.delete("/rbac/roles/{name}")
async def delete_role(
    name: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_RBAC)),
):
    r = (await db.execute(
        select(Role).where(Role.name == name)
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "role-not-found")
    if r.is_system:
        raise HTTPException(400, "cannot-delete-system-role")
    await db.delete(r)
    await db.commit()
    audit_log("admin.rbac.role_deleted", user_id=user_id, success=True,
              details={"role": name})
    _chain_append(user_id, "rbac.role_deleted", name, {})
    return _ok({"role": name})


@router.post("/rbac/roles/{name}/clone")
async def clone_role(
    name: str,
    body: CloneIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_RBAC)),
):
    src = (await db.execute(
        select(Role).where(Role.name == name)
    )).scalar_one_or_none()
    if not src:
        raise HTTPException(404, "source-role-not-found")
    if (await db.execute(
        select(Role).where(Role.name == body.new_name)
    )).scalar_one_or_none():
        raise HTTPException(409, "target-role-exists")
    new = Role(name=body.new_name, description=src.description, is_system=False)
    db.add(new)
    await db.flush()
    src_perms = (await db.execute(
        select(RolePermission).where(RolePermission.role_id == src.id)
    )).scalars().all()
    for p in src_perms:
        db.add(RolePermission(
            role_id=new.id, permission_id=p.permission_id, granted=p.granted,
        ))
    await db.commit()
    audit_log("admin.rbac.role_cloned", user_id=user_id, success=True,
              details={"src": name, "dst": body.new_name})
    return {"id": new.id, "name": new.name}


@router.get("/rbac/roles/{name}/permissions")
async def get_role_permissions(
    name: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_RBAC)),
):
    r = (await db.execute(
        select(Role).where(Role.name == name)
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "role-not-found")
    keys = (await db.execute(
        select(Permission.key)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == r.id)
    )).scalars().all()
    return {"role": name, "permissions": list(keys)}


@router.put("/rbac/roles/{name}/permissions")
async def set_role_permissions(
    name: str,
    body: PermsIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_RBAC)),
):
    r = (await db.execute(
        select(Role).where(Role.name == name)
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "role-not-found")

    # Validate the requested keys against the registry
    valid_keys = set(all_permission_keys())
    requested = set(body.permissions)
    invalid = requested - valid_keys
    if invalid:
        # We still accept arbitrary permission rows if they already
        # exist in the DB (e.g. from a future plugin).
        db_keys = {
            k for (k,) in (await db.execute(select(Permission.key))).all()
        }
        invalid -= db_keys
        if invalid:
            raise HTTPException(400, detail=f"unknown-permissions: {sorted(invalid)}")

    # Wipe existing links, then re-add
    await db.execute(
        RolePermission.__table__.delete().where(RolePermission.role_id == r.id)
    )

    perms_by_key = {
        p.key: p for p in (await db.execute(
            select(Permission).where(Permission.key.in_(requested))
        )).scalars().all()
    }
    for k in requested:
        p = perms_by_key.get(k)
        if p is None:
            continue
        db.add(RolePermission(role_id=r.id, permission_id=p.id, granted=True))
    await db.commit()

    # Invalidate any cached user permission sets that referenced this role
    user_ids = (await db.execute(
        select(UserRole.user_id).where(UserRole.role_id == r.id)
    )).scalars().all()
    for uid in user_ids:
        await rbac_invalidate(uid)

    audit_log("admin.rbac.role_perms_set", user_id=user_id, success=True,
              details={"role": name, "permissions": sorted(requested)})
    _chain_append(user_id, "rbac.role_perms_set", name,
                  {"permissions": sorted(requested)})
    return _ok({"role": name, "permissions": sorted(requested)})


@router.get("/rbac/permissions")
async def list_permissions(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_RBAC)),
):
    rows = (await db.execute(
        select(Permission).order_by(Permission.category, Permission.key)
    )).scalars().all()
    return {
        "items": [
            {"key": p.key, "category": p.category, "description": p.description}
            for p in rows
        ],
        "registry": all_permission_keys(),
    }


# ═══════════════════════════════════════════════════════════════════════
# RBAC — Users
# ═══════════════════════════════════════════════════════════════════════


class UserPatch(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    status: Optional[str] = None
    is_active: Optional[bool] = None
    role: Optional[str] = None


@router.get("/rbac/users")
async def list_rbac_users(
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_RBAC)),
):
    stmt = select(User)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(
            User.username.ilike(like), User.display_name.ilike(like),
        ))
    rows = (await db.execute(
        stmt.order_by(desc(User.created_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "items": [
            {
                "id": u.id, "username": u.username,
                "display_name": u.display_name, "role": u.role,
                "is_active": u.is_active,
                "last_seen": u.last_seen.isoformat() if u.last_seen else None,
            }
            for u in rows
        ],
    }


@router.get("/rbac/users/{uid}")
async def get_rbac_user(
    uid: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_RBAC)),
):
    u = await db.get(User, uid)
    if not u:
        raise HTTPException(404, "user-not-found")
    role_rows = (await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == uid)
    )).scalars().all()
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "role": u.role, "is_active": u.is_active,
        "roles": list(role_rows),
        "last_seen": u.last_seen.isoformat() if u.last_seen else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.put("/rbac/users/{uid}")
async def update_rbac_user(
    uid: str,
    body: UserPatch,
    db: AsyncSession = Depends(get_db),
    actor_id: str = Depends(require_permission(_PERM_USERS)),
):
    u = await db.get(User, uid)
    if not u:
        raise HTTPException(404, "user-not-found")
    if body.display_name is not None:
        u.display_name = body.display_name
    if body.bio is not None:
        u.bio = body.bio
    if body.status is not None:
        u.status = body.status
    if body.is_active is not None:
        u.is_active = body.is_active
    if body.role is not None:
        u.role = body.role
    await db.commit()
    await rbac_invalidate(uid)
    audit_log("admin.rbac.user_updated", user_id=actor_id, success=True,
              details={"user_id": uid})
    return _ok({"user_id": uid})


@router.post("/rbac/users/{uid}/roles/{role}")
async def grant_user_role(
    uid: str,
    role: str,
    db: AsyncSession = Depends(get_db),
    actor_id: str = Depends(require_permission(_PERM_RBAC)),
):
    u = await db.get(User, uid)
    if not u:
        raise HTTPException(404, "user-not-found")
    r = (await db.execute(
        select(Role).where(Role.name == role)
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "role-not-found")
    existing = (await db.execute(
        select(UserRole).where(and_(
            UserRole.user_id == uid, UserRole.role_id == r.id,
        ))
    )).scalar_one_or_none()
    if existing is None:
        db.add(UserRole(user_id=uid, role_id=r.id, assigned_by=actor_id))
    await db.commit()
    await rbac_invalidate(uid)
    audit_log("admin.rbac.role_granted", user_id=actor_id, success=True,
              details={"user_id": uid, "role": role})
    _chain_append(actor_id, "rbac.role_granted", uid, {"role": role})
    return _ok({"user_id": uid, "role": role})


@router.delete("/rbac/users/{uid}/roles/{role}")
async def revoke_user_role(
    uid: str,
    role: str,
    db: AsyncSession = Depends(get_db),
    actor_id: str = Depends(require_permission(_PERM_RBAC)),
):
    r = (await db.execute(
        select(Role).where(Role.name == role)
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "role-not-found")
    await db.execute(
        UserRole.__table__.delete().where(and_(
            UserRole.user_id == uid, UserRole.role_id == r.id,
        ))
    )
    await db.commit()
    await rbac_invalidate(uid)
    audit_log("admin.rbac.role_revoked", user_id=actor_id, success=True,
              details={"user_id": uid, "role": role})
    _chain_append(actor_id, "rbac.role_revoked", uid, {"role": role})
    return _ok({"user_id": uid, "role": role})


@router.post("/rbac/users/{uid}/reset-password")
async def admin_reset_password(
    uid: str,
    db: AsyncSession = Depends(get_db),
    actor_id: str = Depends(require_permission(_PERM_USERS)),
):
    u = await db.get(User, uid)
    if not u:
        raise HTTPException(404, "user-not-found")
    from app.models.billing_license import _temp_password
    temp = _temp_password()
    h = hash_password(temp)
    u.password_hash = h
    pwr = RbacPasswordReset(
        user_id=uid, temp_password_hash=h, issued_by=actor_id,
    )
    db.add(pwr)
    await db.commit()
    audit_log("admin.rbac.password_reset", user_id=actor_id, success=True,
              details={"user_id": uid})
    _chain_append(actor_id, "rbac.password_reset", uid, {})
    return {
        "user_id": uid,
        "temp_password": temp,
        "expires_in": 3600,
        "note": "User must change this on next login.",
    }


@router.post("/rbac/users/{uid}/force-logout")
async def force_logout(
    uid: str,
    db: AsyncSession = Depends(get_db),
    actor_id: str = Depends(require_permission(_PERM_USERS)),
):
    await db.execute(
        UserSession.__table__.delete().where(UserSession.user_id == uid)
    )
    await db.commit()
    audit_log("admin.rbac.force_logout", user_id=actor_id, success=True,
              details={"user_id": uid})
    _chain_append(actor_id, "rbac.force_logout", uid, {})
    return _ok({"user_id": uid})


@router.get("/rbac/users/{uid}/sessions")
async def list_user_sessions(
    uid: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_RBAC)),
):
    rows = (await db.execute(
        select(UserSession).where(UserSession.user_id == uid)
    )).scalars().all()
    return {
        "user_id": uid,
        "sessions": [
            {
                "id": s.id,
                "created_at": getattr(s, "created_at", None).isoformat()
                if getattr(s, "created_at", None) else None,
                "expires_at": getattr(s, "expires_at", None).isoformat()
                if getattr(s, "expires_at", None) else None,
                "user_agent": getattr(s, "user_agent", None),
                "ip_address": getattr(s, "ip_address", None),
            }
            for s in rows
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
# BILLING — Plans (extended with audit history)
# ═══════════════════════════════════════════════════════════════════════


class PlanExtIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    name: str
    description: Optional[str] = None
    price_monthly_cents: int = 0
    price_yearly_cents: int = 0
    currency: str = "USD"
    trial_days: int = 0
    is_public: bool = True
    sort_order: int = 0
    included_quotas: dict[str, Any] = Field(default_factory=dict)
    feature_flags: dict[str, Any] = Field(default_factory=dict)


@router.get("/billing/plans-portal")
async def portal_list_plans(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    rows = (await db.execute(
        select(Plan).order_by(Plan.sort_order, Plan.price_monthly_cents)
    )).scalars().all()
    return {"items": [
        {
            "code": p.slug, "name": p.name,
            "description": p.description,
            "price_monthly_cents": p.price_monthly_cents,
            "price_yearly_cents": p.price_yearly_cents,
            "currency": p.currency, "trial_days": p.trial_days,
            "is_public": p.is_public, "sort_order": p.sort_order,
            "included_quotas": dict(p.included_quotas or {}),
            "feature_flags": dict(p.feature_flags or {}),
        } for p in rows
    ]}


@router.post("/billing/plans-portal", status_code=201)
async def portal_create_plan(
    body: PlanExtIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    existing = (await db.execute(
        select(Plan).where(Plan.slug == body.code)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "plan-exists")
    p = Plan(
        slug=body.code, name=body.name, description=body.description,
        price_monthly_cents=body.price_monthly_cents,
        price_yearly_cents=body.price_yearly_cents,
        currency=body.currency, trial_days=body.trial_days,
        is_public=body.is_public, sort_order=body.sort_order,
        included_quotas=body.included_quotas, feature_flags=body.feature_flags,
    )
    db.add(p)
    db.add(PlanAuditEntry(
        plan_slug=body.code, action="create", actor_id=user_id,
        before_json=None, after_json=body.model_dump(),
    ))
    await db.commit()
    audit_log("admin.plan.created", user_id=user_id, success=True,
              details={"code": body.code})
    _chain_append(user_id, "plan.created", body.code, {})
    return {"code": p.slug, "id": p.id}


@router.put("/billing/plans-portal/{code}")
async def portal_update_plan(
    code: str,
    body: PlanExtIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    p = (await db.execute(
        select(Plan).where(Plan.slug == code)
    )).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "plan-not-found")
    before = {
        "code": p.slug, "name": p.name, "description": p.description,
        "price_monthly_cents": p.price_monthly_cents,
        "price_yearly_cents": p.price_yearly_cents,
        "currency": p.currency, "trial_days": p.trial_days,
        "is_public": p.is_public, "sort_order": p.sort_order,
        "included_quotas": dict(p.included_quotas or {}),
        "feature_flags": dict(p.feature_flags or {}),
    }
    p.name = body.name
    p.description = body.description
    p.price_monthly_cents = body.price_monthly_cents
    p.price_yearly_cents = body.price_yearly_cents
    p.currency = body.currency
    p.trial_days = body.trial_days
    p.is_public = body.is_public
    p.sort_order = body.sort_order
    p.included_quotas = body.included_quotas
    p.feature_flags = body.feature_flags
    db.add(PlanAuditEntry(
        plan_slug=code, action="update", actor_id=user_id,
        before_json=before, after_json=body.model_dump(),
    ))
    await db.commit()
    audit_log("admin.plan.updated", user_id=user_id, success=True,
              details={"code": code})
    return {"code": p.slug}


@router.delete("/billing/plans-portal/{code}")
async def portal_delete_plan(
    code: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    p = (await db.execute(
        select(Plan).where(Plan.slug == code)
    )).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "plan-not-found")
    before = {
        "code": p.slug, "name": p.name, "currency": p.currency,
        "price_monthly_cents": p.price_monthly_cents,
    }
    db.add(PlanAuditEntry(
        plan_slug=code, action="delete", actor_id=user_id,
        before_json=before, after_json=None,
    ))
    try:
        await db.delete(p)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "plan-in-use")
    audit_log("admin.plan.deleted", user_id=user_id, success=True,
              details={"code": code})
    _chain_append(user_id, "plan.deleted", code, {})
    return _ok({"code": code})


@router.get("/billing/plans-portal/{code}/audit")
async def portal_plan_audit(
    code: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    rows = (await db.execute(
        select(PlanAuditEntry).where(PlanAuditEntry.plan_slug == code)
        .order_by(desc(PlanAuditEntry.occurred_at))
    )).scalars().all()
    return {
        "code": code,
        "entries": [
            {
                "id": r.id, "action": r.action, "actor_id": r.actor_id,
                "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
                "before": r.before_json, "after": r.after_json,
            }
            for r in rows
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
# BILLING — Licenses (Ed25519-signed)
# ═══════════════════════════════════════════════════════════════════════


class LicenseIssueIn(BaseModel):
    tenant_id: str
    plan: str
    seats: int = Field(1, ge=1, le=1_000_000)
    duration_days: int = Field(365, ge=1, le=10_000)
    features: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LicenseSignBody(BaseModel):
    payload: dict[str, Any]


class LicenseRenewIn(BaseModel):
    duration_days: int = Field(..., ge=1, le=10_000)


class LicenseValidateIn(BaseModel):
    license: dict[str, Any]
    signature: Optional[str] = None


def _license_dict(lic: BillingLicense) -> dict[str, Any]:
    return {
        "id": lic.id,
        "license_key": lic.license_key,
        "tenant_id": lic.workspace_id,
        "plan": lic.plan_slug,
        "seats": lic.seats,
        "features": dict(lic.features or {}),
        "status": lic.status,
        "issued_at": lic.issued_at.isoformat() if lic.issued_at else None,
        "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
        "revoked_at": lic.revoked_at.isoformat() if lic.revoked_at else None,
        "issued_by": lic.issued_by,
        "payload_sha256": lic.payload_sha256,
        "signature": lic.signature_b64,
    }


@router.get("/billing/licenses")
async def list_licenses(
    tenant_id: Optional[str] = None,
    status_: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    stmt = select(BillingLicense)
    if tenant_id:
        stmt = stmt.where(BillingLicense.workspace_id == tenant_id)
    if status_:
        stmt = stmt.where(BillingLicense.status == status_)
    rows = (await db.execute(
        stmt.order_by(desc(BillingLicense.created_at))
    )).scalars().all()
    return {"items": [_license_dict(l) for l in rows]}


@router.post("/billing/licenses", status_code=201)
async def issue_license(
    body: LicenseIssueIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    ws = await db.get(Workspace, body.tenant_id)
    if not ws:
        raise HTTPException(404, "tenant-not-found")
    signer = get_signer()
    now = utc_now()
    expires = now + timedelta(days=body.duration_days)
    lic_row = BillingLicense(
        workspace_id=body.tenant_id,
        plan_slug=body.plan,
        seats=body.seats,
        features=body.features,
        issued_at=now,
        expires_at=expires,
        issued_by=user_id,
        metadata_json=body.metadata,
        signature_b64="",
        payload_sha256="",
        status="active",
    )
    db.add(lic_row)
    await db.flush()  # need lic_row.license_key (default factory)

    payload = build_license_payload(
        license_key=lic_row.license_key,
        workspace_id=body.tenant_id,
        plan_slug=body.plan,
        seats=body.seats,
        features=body.features,
        issued_at=now,
        expires_at=expires,
        metadata=body.metadata,
    )
    sig = signer.sign_license(payload)
    lic_row.payload_json = payload
    lic_row.signature_b64 = sig
    lic_row.payload_sha256 = sha256_hex(payload)
    lic_row.public_key_pem = signer.export_public_key()
    await db.commit()

    audit_log("admin.license.issued", user_id=user_id, success=True,
              details={"key": lic_row.license_key, "tenant": body.tenant_id})
    _chain_append(user_id, "license.issued", lic_row.license_key,
                  {"plan": body.plan, "seats": body.seats})
    return {
        "license": _license_dict(lic_row),
        "key": lic_row.license_key,
        "payload": payload,
        "signature": sig,
        "fingerprint": signer.export_fingerprint(),
    }


@router.get("/billing/licenses/{key}")
async def get_license(
    key: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    lic = (await db.execute(
        select(BillingLicense).where(BillingLicense.license_key == key)
    )).scalar_one_or_none()
    if not lic:
        raise HTTPException(404, "license-not-found")
    return {"license": _license_dict(lic), "payload": lic.payload_json or {}}


@router.post("/billing/licenses/{key}/revoke")
async def revoke_license(
    key: str,
    reason: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    lic = (await db.execute(
        select(BillingLicense).where(BillingLicense.license_key == key)
    )).scalar_one_or_none()
    if not lic:
        raise HTTPException(404, "license-not-found")
    if lic.status == "revoked":
        return _ok({"license_key": key, "already_revoked": True})
    lic.status = "revoked"
    lic.revoked_at = utc_now()
    lic.revoked_by = user_id
    lic.revoke_reason = reason
    existing = (await db.execute(
        select(LicenseRevocation).where(LicenseRevocation.license_key == key)
    )).scalar_one_or_none()
    if existing is None:
        db.add(LicenseRevocation(
            license_key=key, workspace_id=lic.workspace_id,
            revoked_by=user_id, reason=reason,
            payload_sha256=lic.payload_sha256,
        ))
    await db.commit()
    audit_log("admin.license.revoked", user_id=user_id, success=True,
              details={"key": key, "reason": reason})
    _chain_append(user_id, "license.revoked", key, {"reason": reason})
    return _ok({"license_key": key})


@router.post("/billing/licenses/{key}/renew")
async def renew_license(
    key: str,
    body: LicenseRenewIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    lic = (await db.execute(
        select(BillingLicense).where(BillingLicense.license_key == key)
    )).scalar_one_or_none()
    if not lic:
        raise HTTPException(404, "license-not-found")
    if lic.status == "revoked":
        raise HTTPException(400, "license-revoked")
    base = lic.expires_at if lic.expires_at > utc_now() else utc_now()
    new_expiry = base + timedelta(days=body.duration_days)
    lic.expires_at = new_expiry
    lic.status = "active"

    # Re-sign with extended expiry so offline verifiers see the new date
    signer = get_signer()
    payload = build_license_payload(
        license_key=lic.license_key,
        workspace_id=lic.workspace_id,
        plan_slug=lic.plan_slug,
        seats=lic.seats,
        features=lic.features,
        issued_at=lic.issued_at,
        expires_at=new_expiry,
        metadata=lic.metadata_json,
    )
    lic.payload_json = payload
    lic.signature_b64 = signer.sign_license(payload)
    lic.payload_sha256 = sha256_hex(payload)
    lic.public_key_pem = signer.export_public_key()
    await db.commit()
    audit_log("admin.license.renewed", user_id=user_id, success=True,
              details={"key": key, "days": body.duration_days})
    _chain_append(user_id, "license.renewed", key, {"days": body.duration_days})
    return {"license": _license_dict(lic), "payload": payload}


@router.post("/billing/licenses/{key}/validate")
async def validate_license_by_key(
    key: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    lic = (await db.execute(
        select(BillingLicense).where(BillingLicense.license_key == key)
    )).scalar_one_or_none()
    if not lic:
        raise HTTPException(404, "license-not-found")
    signer = get_signer()
    payload = lic.payload_json or {}
    sig_ok = False
    if lic.public_key_pem:
        sig_ok = signer.verify_license_with_key(payload, lic.signature_b64, lic.public_key_pem)
    if not sig_ok:
        sig_ok = signer.verify_license(payload, lic.signature_b64)
    return {
        "license_key": key,
        "valid": bool(lic.is_valid and sig_ok),
        "signature_ok": sig_ok,
        "status": lic.status,
        "expired": lic.is_expired,
        "revoked": lic.is_revoked,
        "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
    }


@router.post("/billing/licenses/validate")
async def validate_license_payload(
    body: LicenseValidateIn,
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    payload = dict(body.license or {})
    sig = body.signature or payload.pop("signature", None)
    if not sig:
        raise HTTPException(400, "missing-signature")
    signer = get_signer()
    ok = signer.verify_license(payload, sig)
    expires_iso = payload.get("expires_at")
    expired = False
    try:
        if expires_iso:
            expired = datetime.fromisoformat(expires_iso) < datetime.now(timezone.utc)
    except Exception:                                                  # noqa: BLE001
        pass
    return {"valid": ok and not expired, "signature_ok": ok, "expired": expired}


@router.post("/billing/licenses/sign")
async def sign_license_payload(
    body: LicenseSignBody,
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    signer = get_signer()
    sig = signer.sign_license(body.payload)
    audit_log("admin.license.signed", user_id=user_id, success=True,
              details={"sha256": sha256_hex(body.payload)})
    return {
        "signature": sig,
        "fingerprint": signer.export_fingerprint(),
        "payload_sha256": sha256_hex(body.payload),
    }


@router.get("/billing/licenses/{key}/download")
async def download_license(
    key: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    lic = (await db.execute(
        select(BillingLicense).where(BillingLicense.license_key == key)
    )).scalar_one_or_none()
    if not lic:
        raise HTTPException(404, "license-not-found")
    body = {
        "v": 1,
        "license_key": lic.license_key,
        "payload": lic.payload_json or {},
        "signature": lic.signature_b64,
        "public_key_pem": lic.public_key_pem,
        "fingerprint": get_signer().export_fingerprint(),
    }
    blob = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    audit_log("admin.license.downloaded", user_id=user_id, success=True,
              details={"key": key})
    return Response(
        content=blob,
        media_type="application/x-helen-license+json",
        headers={
            "Content-Disposition":
                f'attachment; filename="{lic.license_key}.helen-license"',
        },
    )


@router.get("/billing/licenses-public-key")
async def get_public_key(
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    s = get_signer()
    return {
        "public_key_pem": s.export_public_key(),
        "fingerprint": s.export_fingerprint(),
    }


# ═══════════════════════════════════════════════════════════════════════
# BILLING — Usage
# ═══════════════════════════════════════════════════════════════════════


@router.get("/billing/usage/current")
async def usage_current(
    tenant_id: str,
    period: str = "month",
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    return await UsageMeter.get_current(db, tenant_id, period=period)


@router.get("/billing/usage/history")
async def usage_history(
    tenant_id: str,
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = None,
    endpoint: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    return await UsageMeter.get_history(
        db, tenant_id, from_dt=from_, to_dt=to, endpoint=endpoint,
    )


# ═══════════════════════════════════════════════════════════════════════
# BILLING — Invoices (generate / regenerate / email / pdf)
# ═══════════════════════════════════════════════════════════════════════


class InvoiceGenIn(BaseModel):
    tenant_id: str
    period: Optional[str] = None


class InvoiceEmailIn(BaseModel):
    to: Optional[str] = None


@router.get("/billing/invoices-portal")
async def portal_list_invoices(
    tenant_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    if tenant_id:
        return {"items": await InvoiceGenerator.list_for_tenant(db, tenant_id)}
    rows = (await db.execute(
        select(Invoice).order_by(desc(Invoice.created_at)).limit(200)
    )).scalars().all()
    from app.services.billing.invoice_generator import serialize_invoice
    return {"items": [serialize_invoice(i) for i in rows]}


@router.post("/billing/invoices-portal/generate")
async def portal_invoice_generate(
    body: InvoiceGenIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    inv = await InvoiceGenerator.generate(db, body.tenant_id, period=body.period)
    if inv is None:
        raise HTTPException(404, "no-active-subscription")
    audit_log("admin.invoice.generated", user_id=user_id, success=True,
              details={"invoice_id": inv.id, "tenant_id": body.tenant_id})
    from app.services.billing.invoice_generator import serialize_invoice
    return serialize_invoice(inv)


@router.post("/billing/invoices-portal/{invoice_id}/regenerate")
async def portal_invoice_regenerate(
    invoice_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    new = await InvoiceGenerator.regenerate(db, invoice_id)
    if new is None:
        raise HTTPException(404, "invoice-not-found")
    audit_log("admin.invoice.regenerated", user_id=user_id, success=True,
              details={"old": invoice_id, "new": new.id})
    from app.services.billing.invoice_generator import serialize_invoice
    return serialize_invoice(new)


@router.post("/billing/invoices-portal/{invoice_id}/email")
async def portal_invoice_email(
    invoice_id: str,
    body: InvoiceEmailIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM_BILLING)),
):
    inv = await db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "invoice-not-found")
    sent = await InvoiceGenerator.email(db, inv, to_email=body.to)
    audit_log("admin.invoice.emailed", user_id=user_id, success=sent,
              details={"invoice_id": invoice_id, "to": body.to})
    return _ok({"sent": sent, "invoice_id": invoice_id})


@router.get("/billing/invoices-portal/{invoice_id}/pdf")
async def portal_invoice_pdf(
    invoice_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM_BILLING)),
):
    inv = await db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "invoice-not-found")
    if not inv.pdf_url:
        try:
            await InvoiceGenerator.to_pdf(inv)
            await db.commit()
        except Exception as e:                                         # noqa: BLE001
            logger.error("portal-invoice: pdf render failed err=%s", e)
            raise HTTPException(500, "pdf-render-failed")
    path = Path(inv.pdf_url)
    if not path.exists():
        raise HTTPException(404, "pdf-file-missing")
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else "text/html"
    return FileResponse(path, media_type=media_type, filename=path.name)
