"""
Phase 7 / Module AH — admin plugin endpoints.

Mounted under ``/api/admin/plugins``. Requires ``plugins.admin``.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.plugin import (
    MarketplaceListing,
    PluginEvent,
    PluginManifest,
)
from app.services.plugins.loader import register_manifest
from app.services.plugins.marketplace_client import sync_marketplace_to_db
from app.services.plugins.signer import (
    add_trusted_key,
    generate_keypair,
    list_trusted_keys,
    remove_trusted_key,
)
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin/plugins", tags=["admin-plugins"])


_PERM = "plugins.admin"


# ───────────────────────────────────────────────────────────────────────
# Schemas
# ───────────────────────────────────────────────────────────────────────


class ManifestUploadIn(BaseModel):
    manifest: dict[str, Any]
    code_url: Optional[str] = None


class TrustedKeyIn(BaseModel):
    name: str
    pubkey_b64: str


class ReviewIn(BaseModel):
    reason: Optional[str] = None


# ───────────────────────────────────────────────────────────────────────
# Manifests
# ───────────────────────────────────────────────────────────────────────


@router.get("/manifests")
async def list_manifests(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(PluginManifest)
        .order_by(desc(PluginManifest.created_at))
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {"items": [
        {
            "id": m.id, "slug": m.slug, "name": m.name, "version": m.version,
            "author": m.author, "permissions": list(m.permissions or []),
            "hooks": list(m.hooks_subscribed or []),
            "signed_by": m.signed_by,
            "published_at": m.published_at.isoformat() if m.published_at else None,
        }
        for m in rows
    ]}


@router.post("/manifests/upload")
async def upload_manifest(
    body: ManifestUploadIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        mf = await register_manifest(db, body.manifest, code_url=body.code_url)
    except Exception as e:                                              # noqa: BLE001
        raise HTTPException(400, f"invalid-manifest: {e}")
    audit_log("plugins.manifest.uploaded", user_id=user_id, success=True,
              details={"slug": mf.slug, "version": mf.version})
    return {"id": mf.id, "slug": mf.slug, "version": mf.version}


@router.post("/manifests/{manifest_id}/approve")
async def approve(
    manifest_id: str,
    body: ReviewIn = Body(default_factory=ReviewIn),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    listing = (await db.execute(
        select(MarketplaceListing).where(
            MarketplaceListing.manifest_id == manifest_id,
        )
    )).scalar_one_or_none()
    if listing is None:
        listing = MarketplaceListing(manifest_id=manifest_id,
                                     listing_status="approved")
        db.add(listing)
    else:
        listing.listing_status = "approved"
    await db.commit()
    audit_log("plugins.manifest.approved", user_id=user_id, success=True,
              details={"manifest_id": manifest_id, "reason": body.reason})
    return {"ok": True}


@router.post("/manifests/{manifest_id}/reject")
async def reject(
    manifest_id: str,
    body: ReviewIn = Body(default_factory=ReviewIn),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    listing = (await db.execute(
        select(MarketplaceListing).where(
            MarketplaceListing.manifest_id == manifest_id,
        )
    )).scalar_one_or_none()
    if listing is None:
        listing = MarketplaceListing(manifest_id=manifest_id,
                                     listing_status="rejected")
        db.add(listing)
    else:
        listing.listing_status = "rejected"
    await db.commit()
    audit_log("plugins.manifest.rejected", user_id=user_id, success=True,
              details={"manifest_id": manifest_id, "reason": body.reason})
    return {"ok": True}


@router.get("/marketplace/listings")
async def listings(
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = select(MarketplaceListing, PluginManifest).join(
        PluginManifest, PluginManifest.id == MarketplaceListing.manifest_id,
    )
    if status:
        q = q.where(MarketplaceListing.listing_status == status)
    rows = (await db.execute(q)).all()
    return {"items": [
        {
            "manifest_id": mf.id, "slug": mf.slug, "name": mf.name,
            "status": listing.listing_status, "category": listing.category,
            "downloads": listing.downloads,
            "featured": listing.featured,
        }
        for listing, mf in rows
    ]}


@router.post("/marketplace/sync")
async def sync_marketplace(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission(_PERM)),
):
    stats = await sync_marketplace_to_db(db)
    audit_log("plugins.marketplace.sync", user_id=user_id, success=True,
              details=stats)
    return stats


# ───────────────────────────────────────────────────────────────────────
# Trusted keys
# ───────────────────────────────────────────────────────────────────────


@router.get("/trusted-keys")
async def get_trusted_keys(
    _user: str = Depends(require_permission(_PERM)),
):
    return {"items": list_trusted_keys()}


@router.post("/trusted-keys")
async def add_key(
    body: TrustedKeyIn,
    user_id: str = Depends(require_permission(_PERM)),
):
    add_trusted_key(body.name, body.pubkey_b64)
    audit_log("plugins.trusted_key.added", user_id=user_id, success=True,
              details={"name": body.name})
    return {"ok": True}


@router.delete("/trusted-keys/{name}")
async def remove_key(
    name: str,
    user_id: str = Depends(require_permission(_PERM)),
):
    remove_trusted_key(name)
    audit_log("plugins.trusted_key.removed", user_id=user_id, success=True,
              details={"name": name})
    return {"ok": True}


@router.post("/keypairs")
async def gen_keypair(
    user_id: str = Depends(require_permission(_PERM)),
):
    try:
        priv, pub = generate_keypair()
    except Exception as e:                                              # noqa: BLE001
        raise HTTPException(500, str(e))
    audit_log("plugins.keypair.generated", user_id=user_id, success=True,
              details={})
    return {"private_key_b64": priv, "public_key_b64": pub,
            "note": "store-private-key-offline-only"}


# ───────────────────────────────────────────────────────────────────────
# Audit
# ───────────────────────────────────────────────────────────────────────


@router.get("/audit")
async def audit(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    event: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    q = select(PluginEvent)
    if event:
        q = q.where(PluginEvent.event == event)
    q = q.order_by(desc(PluginEvent.occurred_at)).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return {"items": [
        {
            "id": e.id, "installation_id": e.installation_id,
            "manifest_id": e.manifest_id, "event": e.event,
            "payload": dict(e.payload or {}),
            "duration_ms": e.duration_ms,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        }
        for e in rows
    ]}


# ═══════════════════════════════════════════════════════════════════════
# Phase 7 / Module AH — Plugin Marketplace + Manager extensions
# ═══════════════════════════════════════════════════════════════════════
#
# Adds the full "marketplace + plugin manager" REST + WS surface on top
# of the legacy ``/manifests``, ``/marketplace/*`` and ``/trusted-keys``
# endpoints above. The new endpoints all live under the same
# ``/api/admin/plugins`` prefix and are routed through the same router
# so they are mounted by the existing app wiring.
# ═══════════════════════════════════════════════════════════════════════

import hashlib as _hashlib
import io as _io
import json as _json
import os as _os
import uuid as _uuid
import zipfile as _zipfile
from datetime import datetime as _datetime, timezone as _tz
from pathlib import Path as _Path
from typing import List as _List

from fastapi import (
    File,
    Form,
    Header,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy import func as _sa_func

from app.core.security_utils import require_role as _require_role
from app.models.plugin_job import PluginJob as _PluginJob
from app.models.plugin_rating import PluginRating as _PluginRating
from app.models.plugin_signer import VerifiedSigner as _VerifiedSigner
from app.models.plugin import (
    PluginInstallation as _PluginInstallation,
)
from app.services.plugins.installer import (
    PLUGINS_DIR as _PLUGINS_DIR,
    get_installer as _get_installer,
)
from app.services.plugins.manifest_validator import (
    ManifestValidator as _ManifestValidator,
)
from app.services.plugins.permission_review import (
    review_permissions as _review_permissions,
)
from app.services.plugins.ratings import (
    get_ratings_store as _get_ratings_store,
    rating_dto_to_dict as _rating_dto_to_dict,
)
from app.services.plugins.registry_client import (
    DEFAULT_REGISTRY_URL as _DEFAULT_REGISTRY_URL,
    RegistryClient as _RegistryClient,
    RegistryError as _RegistryError,
    get_registry_client as _get_registry_client,
)
from app.services.plugins.sandbox import (
    get_plugin_sandbox as _get_plugin_sandbox,
)
from app.services.plugins.signer import (
    add_trusted_key as _add_trusted_key,
    list_trusted_keys as _list_trusted_keys,
)
from app.services.plugins.ws_stream import (
    get_plugins_ws_manager as _get_plugins_ws_manager,
)


# In-memory settings store; persisted to disk so changes survive restart.
_SETTINGS_PATH = _Path(_os.getenv(
    "HELEN_PLUGIN_SETTINGS_FILE", "data/plugin-marketplace-settings.json",
))
_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _default_settings() -> dict[str, Any]:
    return {
        "registry_url": _DEFAULT_REGISTRY_URL,
        "airgap": _os.getenv("HELEN_PLUGINS_AIRGAP", "0") == "1",
        "allow_public_registry": False,
        "auto_update": False,
        "auto_update_window": "weekly",
        "sandbox_required": True,
        "network_policy": {
            "allow_outbound": False,
            "allowlist": [],
        },
        "verified_signers_only": False,
    }


def _load_settings() -> dict[str, Any]:
    if not _SETTINGS_PATH.exists():
        cfg = _default_settings()
        _SETTINGS_PATH.write_text(_json.dumps(cfg, indent=2),
                                  encoding="utf-8")
        return cfg
    try:
        cfg = _json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        # Backfill any new keys
        for k, v in _default_settings().items():
            cfg.setdefault(k, v)
        return cfg
    except Exception as e:                                              # noqa: BLE001
        logger.warning("plugin.settings.load failed: %s", e)
        return _default_settings()


def _save_settings(cfg: dict[str, Any]) -> None:
    _SETTINGS_PATH.write_text(_json.dumps(cfg, indent=2), encoding="utf-8")


# ───────────────────────────────────────────────────────────────────────
# Pydantic models
# ───────────────────────────────────────────────────────────────────────


class _InstallIn(BaseModel):
    accept_permissions: bool = False
    explicitly_accepted: list[str] = Field(default_factory=list)
    workspace_id: Optional[str] = None
    notes: Optional[str] = None


class _RatingIn(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    title: Optional[str] = None
    review: Optional[str] = None


class _SettingsIn(BaseModel):
    registry_url: Optional[str] = None
    airgap: Optional[bool] = None
    allow_public_registry: Optional[bool] = None
    auto_update: Optional[bool] = None
    auto_update_window: Optional[str] = None
    sandbox_required: Optional[bool] = None
    network_policy: Optional[dict[str, Any]] = None
    verified_signers_only: Optional[bool] = None


class _RegistryTestIn(BaseModel):
    registry_url: Optional[str] = None


class _SignerIn(BaseModel):
    name: str
    public_key_pem: str
    algorithm: Optional[str] = "ed25519"
    note: Optional[str] = None


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────


def _default_workspace_id(user_id: str) -> str:
    return _os.getenv("HELEN_DEFAULT_WORKSPACE", "default")


def _serialize_installation(row: _PluginInstallation, manifest: Optional[PluginManifest]) -> dict[str, Any]:
    return {
        "installation_id": row.id,
        "manifest_id": row.manifest_id,
        "workspace_id": row.workspace_id,
        "slug": manifest.slug if manifest else None,
        "name": manifest.name if manifest else None,
        "version": manifest.version if manifest else None,
        "status": row.status,
        "installed_at": row.installed_at.isoformat() if row.installed_at else None,
        "installed_by": row.installed_by,
        "error": row.error_message,
        "last_invoked_at": row.last_invoked_at.isoformat() if row.last_invoked_at else None,
        "permissions": list((manifest.permissions if manifest else None) or []),
        "hooks": list((manifest.hooks_subscribed if manifest else None) or []),
    }


def _serialize_manifest(m: PluginManifest) -> dict[str, Any]:
    return {
        "id": m.id, "slug": m.slug, "name": m.name, "version": m.version,
        "author": m.author, "description": m.description,
        "homepage": m.homepage,
        "min_helen_version": m.min_helen_version,
        "max_helen_version": m.max_helen_version,
        "permissions": list(m.permissions or []),
        "hooks_subscribed": list(m.hooks_subscribed or []),
        "ui_routes": list(m.ui_routes or []),
        "settings_schema": dict(m.settings_schema or {}),
        "dependencies": list(m.dependencies or []),
        "entrypoint": m.entrypoint,
        "code_url": m.code_url,
        "code_sha256": m.code_sha256,
        "signed_by": m.signed_by,
        "published_at": m.published_at.isoformat() if m.published_at else None,
    }


def _serialize_job(j: _PluginJob) -> dict[str, Any]:
    return {
        "id": j.id, "slug": j.slug, "version": j.version,
        "kind": j.kind, "state": j.state, "phase": j.phase, "pct": j.pct,
        "actor_id": j.actor_id,
        "detail": dict(j.detail or {}),
        "error": j.error_message,
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


# ═══════════════════════════════════════════════════════════════════════
# Catalog / installed / categories
# ═══════════════════════════════════════════════════════════════════════


@router.get("/registry")
async def list_registry(
    category: Optional[str] = None,
    tag: Optional[str] = None,
    search: Optional[str] = None,
    sort: str = "downloads",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _user: str = Depends(_require_role("admin")),
):
    """Browse the LAN plugin registry."""
    client = _get_registry_client()
    try:
        catalog = await client.fetch_catalog(
            category=category, tag=tag, search=search, sort=sort,
            page=page, page_size=page_size,
        )
    except _RegistryError as e:
        raise HTTPException(502, f"registry-error: {e}")
    return {
        "items": [
            {
                "slug": c.slug, "name": c.name, "version": c.version,
                "author": c.author, "description": c.description,
                "category": c.category, "tags": c.tags,
                "rating_avg": c.rating_avg, "ratings_count": c.ratings_count,
                "downloads": c.downloads, "signed_by": c.signed_by,
                "homepage": c.homepage, "icon": c.icon,
                "screenshots": c.screenshots,
                "long_description": c.long_description,
            }
            for c in catalog.items
        ],
        "total": catalog.total,
        "page": catalog.page, "page_size": catalog.page_size,
        "categories": catalog.categories,
    }


@router.get("/installed")
async def list_installed(
    workspace_id: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    ws_id = workspace_id or _default_workspace_id(user_id)
    q = select(_PluginInstallation, PluginManifest).join(
        PluginManifest, PluginManifest.id == _PluginInstallation.manifest_id,
    ).where(_PluginInstallation.workspace_id == ws_id)
    if status_filter:
        q = q.where(_PluginInstallation.status == status_filter)
    rows = (await db.execute(q)).all()
    return {
        "workspace_id": ws_id,
        "items": [_serialize_installation(inst, mf) for inst, mf in rows],
    }


@router.get("/categories")
async def list_categories(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(_require_role("admin")),
):
    rows = (await db.execute(
        select(
            MarketplaceListing.category,
            _sa_func.count(MarketplaceListing.id),
        ).group_by(MarketplaceListing.category)
    )).all()
    cats = [
        {"category": r[0] or "uncategorized", "count": int(r[1])}
        for r in rows
    ]
    return {"items": cats}


# ═══════════════════════════════════════════════════════════════════════
# Install / uninstall / enable / disable / upgrade
# ═══════════════════════════════════════════════════════════════════════


@router.post("/{slug}/install")
async def install_plugin_endpoint(
    slug: str,
    body: _InstallIn,
    version: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    ws_id = body.workspace_id or _default_workspace_id(user_id)
    # Try to resolve "latest" from existing manifests if version not given
    if not version:
        mf = (await db.execute(
            select(PluginManifest).where(PluginManifest.slug == slug)
            .order_by(desc(PluginManifest.created_at)).limit(1)
        )).scalar_one_or_none()
        version = mf.version if mf else "latest"

    installer = _get_installer()
    audit_log("plugins.install.requested", user_id=user_id, success=True,
              details={"slug": slug, "version": version,
                       "accept_permissions": body.accept_permissions})
    result = await installer.install(
        db, slug=slug, version=version,
        workspace_id=ws_id, actor_id=user_id,
        accept_permissions=body.accept_permissions,
        accepted_codes=body.explicitly_accepted,
    )
    if not result.ok:
        audit_log("plugins.install.failed", user_id=user_id, success=False,
                  details={"slug": slug, "version": version,
                           "error": result.error})
        raise HTTPException(400, result.error or "install-failed")
    audit_log("plugins.install.completed", user_id=user_id, success=True,
              details={"slug": slug, "version": version,
                       "job_id": result.job_id,
                       "installation_id": result.installation_id})
    return {
        "ok": True, "job_id": result.job_id,
        "installation_id": result.installation_id,
        "phases": result.phases, "duration_ms": result.duration_ms,
    }


@router.post("/{slug}/uninstall")
async def uninstall_plugin_endpoint(
    slug: str,
    workspace_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    ws_id = workspace_id or _default_workspace_id(user_id)
    installer = _get_installer()
    result = await installer.uninstall(
        db, slug=slug, workspace_id=ws_id, actor_id=user_id,
    )
    audit_log("plugins.uninstall", user_id=user_id, success=result.ok,
              details={"slug": slug, "job_id": result.job_id,
                       "error": result.error})
    if not result.ok:
        raise HTTPException(400, result.error or "uninstall-failed")
    return {"ok": True, "job_id": result.job_id}


async def _toggle_install_status(
    db: AsyncSession, slug: str, workspace_id: str,
    *, enabled: bool, user_id: str,
) -> dict[str, Any]:
    mf = (await db.execute(
        select(PluginManifest).where(PluginManifest.slug == slug)
        .order_by(desc(PluginManifest.created_at)).limit(1)
    )).scalar_one_or_none()
    if not mf:
        raise HTTPException(404, "plugin-not-found")
    inst = (await db.execute(
        select(_PluginInstallation).where(
            _PluginInstallation.workspace_id == workspace_id,
            _PluginInstallation.manifest_id == mf.id,
        )
    )).scalar_one_or_none()
    if not inst:
        raise HTTPException(404, "plugin-not-installed")
    inst.status = "installed" if enabled else "disabled"
    db.add(PluginEvent(
        installation_id=inst.id,
        event="enable" if enabled else "disable",
        payload={"by": user_id},
    ))
    await db.commit()
    from app.services.plugins.loader import (
        load_plugin as _load_plugin,
    )
    from app.services.plugins.hooks import (
        unregister_installation as _unreg,
    )
    if enabled:
        await _load_plugin(db, inst.id)
    else:
        await _unreg(inst.id)
    return {"ok": True, "status": inst.status}


@router.post("/{slug}/enable")
async def enable_plugin_endpoint(
    slug: str,
    workspace_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    ws_id = workspace_id or _default_workspace_id(user_id)
    out = await _toggle_install_status(
        db, slug, ws_id, enabled=True, user_id=user_id,
    )
    audit_log("plugins.enable", user_id=user_id, success=True,
              details={"slug": slug, "workspace_id": ws_id})
    return out


@router.post("/{slug}/disable")
async def disable_plugin_endpoint(
    slug: str,
    workspace_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    ws_id = workspace_id or _default_workspace_id(user_id)
    out = await _toggle_install_status(
        db, slug, ws_id, enabled=False, user_id=user_id,
    )
    audit_log("plugins.disable", user_id=user_id, success=True,
              details={"slug": slug, "workspace_id": ws_id})
    return out


@router.post("/{slug}/upgrade")
async def upgrade_plugin_endpoint(
    slug: str,
    to: str = Query(..., description="Target version (semver)"),
    body: _InstallIn = Body(default_factory=_InstallIn),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    ws_id = body.workspace_id or _default_workspace_id(user_id)
    installer = _get_installer()
    result = await installer.upgrade(
        db, slug=slug, to_version=to,
        workspace_id=ws_id, actor_id=user_id,
        accept_permissions=body.accept_permissions,
        accepted_codes=body.explicitly_accepted,
    )
    audit_log("plugins.upgrade", user_id=user_id, success=result.ok,
              details={"slug": slug, "to": to, "job_id": result.job_id,
                       "error": result.error})
    if not result.ok:
        raise HTTPException(400, result.error or "upgrade-failed")
    return {
        "ok": True, "job_id": result.job_id,
        "installation_id": result.installation_id,
        "duration_ms": result.duration_ms,
    }


# ═══════════════════════════════════════════════════════════════════════
# Manifest / permissions / logs
# ═══════════════════════════════════════════════════════════════════════


@router.get("/{slug}/manifest")
async def get_manifest_endpoint(
    slug: str,
    version: Optional[str] = None,
    source: str = Query(
        "auto",
        regex="^(auto|local|registry)$",
        description="Where to read the manifest from.",
    ),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(_require_role("admin")),
):
    # local first
    if source in ("auto", "local"):
        q = select(PluginManifest).where(PluginManifest.slug == slug)
        if version:
            q = q.where(PluginManifest.version == version)
        q = q.order_by(desc(PluginManifest.created_at)).limit(1)
        mf = (await db.execute(q)).scalar_one_or_none()
        if mf:
            return {"source": "local", "manifest": _serialize_manifest(mf)}
        if source == "local":
            raise HTTPException(404, "manifest-not-local")
    # registry
    try:
        client = _get_registry_client()
        manifest_dict = await client.fetch_manifest(slug, version or "latest")
    except _RegistryError as e:
        raise HTTPException(502, f"registry-error: {e}")
    return {"source": "registry", "manifest": manifest_dict}


@router.get("/{slug}/permissions")
async def get_permissions_endpoint(
    slug: str,
    version: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(_require_role("admin")),
):
    q = select(PluginManifest).where(PluginManifest.slug == slug)
    if version:
        q = q.where(PluginManifest.version == version)
    q = q.order_by(desc(PluginManifest.created_at)).limit(1)
    mf = (await db.execute(q)).scalar_one_or_none()
    perms: list[str] = []
    if mf:
        perms = list(mf.permissions or [])
    else:
        # fall back to registry
        try:
            data = await _get_registry_client().fetch_manifest(
                slug, version or "latest",
            )
            perms = list(data.get("permissions") or [])
        except _RegistryError:
            raise HTTPException(404, "permissions-unavailable")
    infos = _review_permissions(perms)
    out = [
        {
            "code": i.code, "severity": i.severity,
            "description": i.description,
            "requires_explicit_accept": i.requires_explicit_accept,
        }
        for i in infos
    ]
    summary = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for i in infos:
        summary[i.severity] = summary.get(i.severity, 0) + 1
    return {"items": out, "summary": summary}


@router.get("/{slug}/logs")
async def get_logs_endpoint(
    slug: str,
    level: Optional[str] = None,
    tail: int = Query(200, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(_require_role("admin")),
):
    # Find installation(s) by slug → events
    mf_ids = [
        r[0] for r in (await db.execute(
            select(PluginManifest.id).where(PluginManifest.slug == slug)
        )).all()
    ]
    if not mf_ids:
        return {"items": []}
    inst_ids = [
        r[0] for r in (await db.execute(
            select(_PluginInstallation.id)
            .where(_PluginInstallation.manifest_id.in_(mf_ids))
        )).all()
    ]
    if not inst_ids:
        return {"items": []}
    q = select(PluginEvent).where(PluginEvent.installation_id.in_(inst_ids))
    if level:
        # store-level mapping: hook_error / error → "error"
        if level.lower() == "error":
            q = q.where(PluginEvent.event.in_(("hook_error", "error",
                                              "signature_failed",
                                              "sandbox_violation")))
        elif level.lower() == "warn":
            q = q.where(PluginEvent.event.in_(("hook_error",)))
    q = q.order_by(desc(PluginEvent.occurred_at)).limit(tail)
    rows = (await db.execute(q)).scalars().all()
    return {"items": [
        {
            "id": e.id, "event": e.event, "payload": dict(e.payload or {}),
            "duration_ms": e.duration_ms,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        }
        for e in rows
    ]}


# ═══════════════════════════════════════════════════════════════════════
# Ratings (internal only)
# ═══════════════════════════════════════════════════════════════════════


@router.get("/{slug}/ratings")
async def list_ratings_endpoint(
    slug: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(_require_role("admin")),
):
    store = _get_ratings_store()
    dtos = await store.list_for_plugin(db, slug, limit=limit, offset=offset)
    agg = await store.aggregate(db, slug)
    return {
        "items": [_rating_dto_to_dict(d) for d in dtos],
        "aggregate": {
            "average": agg.average, "count": agg.count,
            "histogram": agg.histogram,
        },
    }


@router.post("/{slug}/ratings")
async def post_rating_endpoint(
    slug: str,
    body: _RatingIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    store = _get_ratings_store()
    try:
        dto = await store.upsert(
            db, slug=slug, user_id=user_id,
            rating=body.rating, title=body.title, review=body.review,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_log("plugins.rating.posted", user_id=user_id, success=True,
              details={"slug": slug, "rating": body.rating})
    return _rating_dto_to_dict(dto)


@router.delete("/{slug}/ratings")
async def delete_rating_endpoint(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    deleted = await _get_ratings_store().delete(
        db, slug=slug, user_id=user_id,
    )
    audit_log("plugins.rating.deleted", user_id=user_id, success=deleted,
              details={"slug": slug})
    return {"ok": deleted}


# ═══════════════════════════════════════════════════════════════════════
# Upload + sandbox preview
# ═══════════════════════════════════════════════════════════════════════


@router.post("/upload")
async def upload_plugin_bundle(
    file: UploadFile = File(...),
    install_after: bool = Form(False),
    workspace_id: Optional[str] = Form(None),
    accept_permissions: bool = Form(False),
    explicitly_accepted: Optional[str] = Form(
        None,
        description="Comma-separated permission codes",
    ),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    """Accept a .helen-plugin / .zip / .whl upload."""
    filename = (file.filename or "").lower()
    if not filename.endswith((".helen-plugin", ".zip", ".whl")):
        raise HTTPException(400, "unsupported-extension")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty-bundle")
    # Try to read manifest from inside the zip
    manifest_dict: dict[str, Any] = {}
    try:
        with _zipfile.ZipFile(_io.BytesIO(data), "r") as zf:
            if "plugin.json" in zf.namelist():
                manifest_dict = _json.loads(
                    zf.read("plugin.json").decode("utf-8"),
                )
    except Exception:                                                       # noqa: BLE001
        pass
    slug = manifest_dict.get("slug") or _Path(filename).stem
    version = manifest_dict.get("version") or "0.0.0"
    sha = _hashlib.sha256(data).hexdigest()
    manifest_dict.setdefault("code_sha256", sha)
    audit_log("plugins.upload", user_id=user_id, success=True,
              details={"slug": slug, "version": version,
                       "size": len(data), "sha256": sha})
    if not install_after:
        # Just stage to the registry-client cache and return a job-less ID
        client = _get_registry_client()
        bundle = client.import_bundle(slug, version, data,
                                      expected_sha256=manifest_dict.get(
                                          "code_sha256"))
        return {
            "ok": True, "slug": slug, "version": version,
            "sha256": bundle.sha256, "size": bundle.size,
            "staged_at": str(bundle.path),
        }
    # Install path
    explicit = (
        [p.strip() for p in (explicitly_accepted or "").split(",") if p.strip()]
    )
    ws_id = workspace_id or _default_workspace_id(user_id)
    installer = _get_installer()
    result = await installer.install(
        db, slug=slug, version=version,
        workspace_id=ws_id, actor_id=user_id,
        accept_permissions=accept_permissions,
        accepted_codes=explicit,
        uploaded_bundle=data,
        uploaded_manifest=manifest_dict,
    )
    if not result.ok:
        raise HTTPException(400, result.error or "install-failed")
    return {
        "ok": True, "job_id": result.job_id, "slug": slug, "version": version,
        "installation_id": result.installation_id,
    }


@router.post("/{slug}/sandbox-preview")
async def sandbox_preview_endpoint(
    slug: str,
    version: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    """Install the plugin into a disposable sandbox and run the entry point."""
    client = _get_registry_client()
    try:
        manifest = await client.fetch_manifest(slug, version or "latest")
        bundle = await client.fetch_bundle(
            slug, manifest.get("version") or version or "latest",
            expected_sha256=manifest.get("code_sha256"),
            signed_by=manifest.get("signed_by"),
        )
    except _RegistryError as e:
        raise HTTPException(502, f"registry-error: {e}")
    sandbox = _get_plugin_sandbox()
    try:
        install = sandbox.install_from_bundle(
            slug, manifest.get("version") or version or "latest",
            bundle.path, manifest_dict=manifest,
        )
        report = sandbox.run_entrypoint(install)
    except Exception as e:                                              # noqa: BLE001
        raise HTTPException(400, f"sandbox-error: {e}")
    audit_log("plugins.sandbox_preview", user_id=user_id, success=report.ok,
              details={"slug": slug, "method": report.isolation_method,
                       "duration_ms": report.duration_ms,
                       "exit_code": report.exit_code})
    return {
        "ok": report.ok,
        "isolation_method": report.isolation_method,
        "duration_ms": report.duration_ms,
        "exit_code": report.exit_code,
        "stdout": report.stdout,
        "stderr": report.stderr,
        "error": report.error,
        "install_dir": str(install.install_dir),
    }


# ═══════════════════════════════════════════════════════════════════════
# Jobs
# ═══════════════════════════════════════════════════════════════════════


@router.get("/jobs/{job_id}")
async def get_job_endpoint(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(_require_role("admin")),
):
    job = (await db.execute(
        select(_PluginJob).where(_PluginJob.id == job_id)
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "job-not-found")
    return _serialize_job(job)


@router.get("/jobs")
async def list_jobs_endpoint(
    state: Optional[str] = None,
    slug: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(_require_role("admin")),
):
    q = select(_PluginJob)
    if state:
        q = q.where(_PluginJob.state == state)
    if slug:
        q = q.where(_PluginJob.slug == slug)
    q = q.order_by(desc(_PluginJob.created_at)) \
        .offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()
    return {"items": [_serialize_job(j) for j in rows]}


# ═══════════════════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════════════════


@router.get("/settings")
async def get_settings_endpoint(
    _user: str = Depends(_require_role("admin")),
):
    return _load_settings()


@router.put("/settings")
async def put_settings_endpoint(
    body: _SettingsIn,
    user_id: str = Depends(_require_role("admin")),
):
    cfg = _load_settings()
    for k in (
        "registry_url", "airgap", "allow_public_registry",
        "auto_update", "auto_update_window", "sandbox_required",
        "network_policy", "verified_signers_only",
    ):
        v = getattr(body, k, None)
        if v is not None:
            cfg[k] = v
    _save_settings(cfg)
    audit_log("plugins.settings.updated", user_id=user_id, success=True,
              details={"changed_keys": [k for k in cfg.keys()]})
    return cfg


@router.post("/settings/test-registry")
async def test_registry_endpoint(
    body: _RegistryTestIn = Body(default_factory=_RegistryTestIn),
    user_id: str = Depends(_require_role("admin")),
):
    cfg = _load_settings()
    url = body.registry_url or cfg.get("registry_url") or _DEFAULT_REGISTRY_URL
    async with _RegistryClient(base_url=url) as client:
        result = await client.ping()
    audit_log("plugins.settings.test_registry", user_id=user_id,
              success=bool(result.get("ok")),
              details={"url": url, "result": result})
    return result


@router.post("/settings/signers")
async def add_signer_endpoint(
    body: _SignerIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(_require_role("admin")),
):
    # Compute fingerprint
    fingerprint = _hashlib.sha256(
        body.public_key_pem.encode("utf-8"),
    ).hexdigest()[:32]
    existing = (await db.execute(
        select(_VerifiedSigner).where(_VerifiedSigner.name == body.name)
    )).scalar_one_or_none()
    if existing:
        existing.public_key_pem = body.public_key_pem
        existing.algorithm = body.algorithm or "ed25519"
        existing.fingerprint = fingerprint
        existing.note = body.note
        existing.added_by = user_id
        row = existing
    else:
        row = _VerifiedSigner(
            name=body.name,
            public_key_pem=body.public_key_pem,
            algorithm=body.algorithm or "ed25519",
            fingerprint=fingerprint,
            note=body.note,
            added_by=user_id,
        )
        db.add(row)
    await db.commit()
    # Mirror to legacy file trust store so signer.verify_against_trust_store
    # picks it up immediately.
    try:
        # Best-effort: strip PEM headers to get base64.
        b64 = "".join(
            line.strip()
            for line in body.public_key_pem.splitlines()
            if line.strip() and not line.startswith("-----")
        )
        _add_trusted_key(body.name, b64)
    except Exception as e:                                              # noqa: BLE001
        logger.warning("signer.legacy-mirror-failed: %s", e)
    audit_log("plugins.signer.added", user_id=user_id, success=True,
              details={"name": body.name, "fingerprint": fingerprint})
    return {
        "id": row.id, "name": row.name, "fingerprint": fingerprint,
        "algorithm": row.algorithm, "added_by": row.added_by,
    }


@router.get("/settings/signers")
async def list_signers_endpoint(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(_require_role("admin")),
):
    rows = (await db.execute(
        select(_VerifiedSigner).order_by(_VerifiedSigner.created_at)
    )).scalars().all()
    return {
        "db_signers": [
            {
                "id": r.id, "name": r.name, "algorithm": r.algorithm,
                "fingerprint": r.fingerprint, "note": r.note,
                "added_by": r.added_by,
                "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
            }
            for r in rows
        ],
        "file_signers": _list_trusted_keys(),
    }


# ═══════════════════════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════════════════════


_PLUGIN_WS_PATH = "/ws/plugins"


@router.websocket(_PLUGIN_WS_PATH)
async def plugins_ws(
    ws: WebSocket,
    job_id: Optional[str] = None,
    slug: Optional[str] = None,
    token: Optional[str] = None,
):
    """WS push for install / upgrade / uninstall / sandbox progress.

    Authenticate via ``?token=<bearer>``. The token must carry the
    admin role.
    """
    # Auth — re-use the standard bearer validator
    try:
        from app.core.security import decode_token as _decode_token
        from app.core.security_utils import has_role as _has_role
        if not token:
            await ws.close(code=4401)
            return
        payload = _decode_token(token)
        if payload.get("type") != "access":
            await ws.close(code=4401)
            return
        role = payload.get("role", "user")
        if not _has_role(role, "admin"):
            await ws.close(code=4403)
            return
    except Exception:                                                   # noqa: BLE001
        await ws.close(code=4401)
        return

    mgr = _get_plugins_ws_manager()
    filters: dict[str, Any] = {}
    if job_id:
        filters["job_id"] = job_id
    if slug:
        filters["slug"] = slug
    sub = await mgr.connect(ws, filters=filters)
    try:
        await mgr.pump(sub)
    except WebSocketDisconnect:
        pass
    finally:
        await mgr.disconnect(sub)
