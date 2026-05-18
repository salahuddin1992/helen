"""
Phase 7 / Module AH — user-facing plugin endpoints.

Mounted under ``/api/plugins``. Scoped to the caller's workspace.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.plugin import (
    PluginEvent,
    PluginInstallation,
    PluginManifest,
    PluginPermissionGrant,
)
from app.models.workspace import WorkspaceMember
from app.services.plugins.loader import (
    install_plugin,
    set_enabled,
    uninstall_plugin,
)
from app.services.plugins.marketplace_client import browse_marketplace

logger = get_logger(__name__)
router = APIRouter(prefix="/api/plugins", tags=["plugins"])


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────


async def _ws(db: AsyncSession, user_id: str) -> str:
    wid = (await db.execute(
        select(WorkspaceMember.workspace_id).where(
            WorkspaceMember.user_id == user_id,
        ).limit(1)
    )).scalar_one_or_none()
    if not wid:
        raise HTTPException(404, "no-workspace")
    return wid


async def _load_owned(
    db: AsyncSession, installation_id: str, workspace_id: str,
) -> PluginInstallation:
    inst = (await db.execute(
        select(PluginInstallation).where(
            PluginInstallation.id == installation_id,
            PluginInstallation.workspace_id == workspace_id,
        )
    )).scalar_one_or_none()
    if not inst:
        raise HTTPException(404, "installation-not-found")
    return inst


# ───────────────────────────────────────────────────────────────────────
# Schemas
# ───────────────────────────────────────────────────────────────────────


class InstallIn(BaseModel):
    slug: Optional[str] = None
    manifest_id: Optional[str] = None
    version: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)


class ConfigIn(BaseModel):
    config: dict[str, Any]


class PermissionIn(BaseModel):
    permission: str
    granted: bool


# ───────────────────────────────────────────────────────────────────────
# Marketplace
# ───────────────────────────────────────────────────────────────────────


@router.get("/marketplace")
async def marketplace(
    q: Optional[str] = None,
    category: Optional[str] = None,
    featured: bool = Query(False),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(get_current_user_id),
):
    items = await browse_marketplace(
        db, q=q, category=category, featured_only=featured,
        limit=limit, offset=offset,
    )
    return {"items": items}


@router.get("/marketplace/{slug}")
async def marketplace_detail(
    slug: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(get_current_user_id),
):
    items = await browse_marketplace(db, q=slug, limit=5)
    hit = next((x for x in items if x["slug"] == slug), None)
    if not hit:
        raise HTTPException(404, "plugin-not-found")
    return hit


# ───────────────────────────────────────────────────────────────────────
# Installed plugins
# ───────────────────────────────────────────────────────────────────────


@router.get("/installed")
async def list_installed(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    rows = (await db.execute(
        select(PluginInstallation, PluginManifest)
        .join(PluginManifest, PluginManifest.id == PluginInstallation.manifest_id)
        .where(PluginInstallation.workspace_id == wid)
        .order_by(desc(PluginInstallation.installed_at))
    )).all()
    return {"items": [
        {
            "id": inst.id, "slug": mf.slug, "name": mf.name,
            "version": mf.version, "status": inst.status,
            "installed_at": inst.installed_at.isoformat(),
            "installed_by": inst.installed_by,
            "config": dict(inst.config or {}),
            "permissions": list(mf.permissions or []),
            "error": inst.error_message,
        }
        for inst, mf in rows
    ]}


@router.post("/install")
async def install(
    body: InstallIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    if not body.manifest_id and not body.slug:
        raise HTTPException(400, "slug-or-manifest_id-required")
    if body.manifest_id:
        mf = (await db.execute(
            select(PluginManifest).where(PluginManifest.id == body.manifest_id)
        )).scalar_one_or_none()
    else:
        q = select(PluginManifest).where(PluginManifest.slug == body.slug)
        if body.version:
            q = q.where(PluginManifest.version == body.version)
        q = q.order_by(desc(PluginManifest.published_at))
        mf = (await db.execute(q)).scalars().first()
    if not mf:
        raise HTTPException(404, "manifest-not-found")
    try:
        inst = await install_plugin(
            db, workspace_id=wid, manifest_id=mf.id,
            user_id=user_id, config=body.config,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_log("plugins.install", user_id=user_id, success=True,
              details={"slug": mf.slug, "install_id": inst.id})
    return {"id": inst.id, "slug": mf.slug, "version": mf.version}


@router.delete("/installed/{installation_id}")
async def uninstall(
    installation_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    await _load_owned(db, installation_id, wid)
    await uninstall_plugin(db, installation_id)
    audit_log("plugins.uninstall", user_id=user_id, success=True,
              details={"install_id": installation_id})
    return {"ok": True}


@router.patch("/installed/{installation_id}/enable")
async def enable(
    installation_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    await _load_owned(db, installation_id, wid)
    await set_enabled(db, installation_id, enabled=True)
    return {"ok": True, "status": "installed"}


@router.patch("/installed/{installation_id}/disable")
async def disable(
    installation_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    await _load_owned(db, installation_id, wid)
    await set_enabled(db, installation_id, enabled=False)
    return {"ok": True, "status": "disabled"}


@router.patch("/installed/{installation_id}/config")
async def update_config(
    installation_id: str,
    body: ConfigIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    inst = await _load_owned(db, installation_id, wid)
    inst.config = body.config
    db.add(PluginEvent(
        installation_id=inst.id, event="config_changed",
        payload={"keys": list(body.config.keys())},
    ))
    await db.commit()
    return {"ok": True}


@router.post("/installed/{installation_id}/permissions")
async def toggle_permission(
    installation_id: str,
    body: PermissionIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    inst = await _load_owned(db, installation_id, wid)
    grant = (await db.execute(
        select(PluginPermissionGrant).where(
            PluginPermissionGrant.installation_id == inst.id,
            PluginPermissionGrant.permission == body.permission,
        )
    )).scalar_one_or_none()
    if grant is None:
        grant = PluginPermissionGrant(
            installation_id=inst.id, permission=body.permission,
            granted=body.granted, granted_by=user_id,
        )
        db.add(grant)
    else:
        grant.granted = body.granted
    db.add(PluginEvent(
        installation_id=inst.id,
        event="permission_granted" if body.granted else "permission_revoked",
        payload={"permission": body.permission},
    ))
    await db.commit()
    return {"ok": True, "permission": body.permission, "granted": body.granted}


@router.get("/installed/{installation_id}/logs")
async def installation_logs(
    installation_id: str,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    await _load_owned(db, installation_id, wid)
    rows = (await db.execute(
        select(PluginEvent).where(PluginEvent.installation_id == installation_id)
        .order_by(desc(PluginEvent.occurred_at)).limit(limit)
    )).scalars().all()
    return {"items": [
        {
            "id": e.id, "event": e.event, "payload": dict(e.payload or {}),
            "duration_ms": e.duration_ms,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        }
        for e in rows
    ]}
