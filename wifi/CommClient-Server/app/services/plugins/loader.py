"""
Plugin loader — install / load / unload pipeline.

Lifecycle:

  1. Manifest is validated (``manifest_schema.parse_manifest``)
  2. Signature is verified against the trust store
  3. Helen-version range is checked
  4. Code is fetched (``code_url`` or local file)
  5. PluginInstallation row is created and the plugin is compiled in
     :mod:`sandbox`. Any ``hooks_subscribed`` hooks register handlers
     that wrap the sandboxed code with a fresh SDK context.
  6. A small in-memory cache (TTL 10 minutes) speeds up repeated loads.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.plugin import (
    PluginEvent,
    PluginInstallation,
    PluginManifest,
    PluginPermissionGrant,
)
from app.services.plugins.helen_sdk import SDKContext, make_namespace
from app.services.plugins.hooks import register_hook, unregister_installation
from app.services.plugins.manifest_schema import Manifest, parse_manifest
from app.services.plugins.sandbox import run_plugin_code
from app.services.plugins.signer import verify_against_trust_store

logger = get_logger(__name__)


HELEN_VERSION = "7.0.0"
CACHE_TTL_SEC = 600


# ───────────────────────────────────────────────────────────────────────
# Plugin code cache
# ───────────────────────────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    source: str
    fetched_at: float


_code_cache: dict[str, _CacheEntry] = {}
_cache_lock = asyncio.Lock()


async def _fetch_code(manifest: PluginManifest) -> str:
    key = f"{manifest.slug}@{manifest.version}"
    async with _cache_lock:
        e = _code_cache.get(key)
        if e and (time.time() - e.fetched_at < CACHE_TTL_SEC):
            return e.source
    src = ""
    if manifest.code_url and manifest.code_url.startswith(("file://", "/")):
        path = Path(manifest.code_url.removeprefix("file://"))
        if path.exists():
            src = path.read_text(encoding="utf-8")
    elif manifest.code_url and manifest.code_url.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(manifest.code_url, timeout=15) as resp:
                src = resp.read(2 * 1024 * 1024).decode("utf-8")
        except Exception as e:                                              # noqa: BLE001
            logger.warning("plugin.fetch failed %s: %s", manifest.code_url, e)
    async with _cache_lock:
        _code_cache[key] = _CacheEntry(src, time.time())
    return src


# ───────────────────────────────────────────────────────────────────────
# Version compat
# ───────────────────────────────────────────────────────────────────────


def _semver_tuple(v: str) -> tuple[int, ...]:
    try:
        core = v.split("+", 1)[0].split("-", 1)[0]
        return tuple(int(p) for p in core.split("."))
    except Exception:                                                   # noqa: BLE001
        return (0,)


def _version_ok(mf: PluginManifest) -> tuple[bool, Optional[str]]:
    cur = _semver_tuple(HELEN_VERSION)
    if mf.min_helen_version:
        if cur < _semver_tuple(mf.min_helen_version):
            return False, f"requires helen>={mf.min_helen_version}"
    if mf.max_helen_version:
        if cur > _semver_tuple(mf.max_helen_version):
            return False, f"incompatible with helen>{mf.max_helen_version}"
    return True, None


# ───────────────────────────────────────────────────────────────────────
# Install / uninstall API
# ───────────────────────────────────────────────────────────────────────


async def install_plugin(
    db: AsyncSession,
    *,
    workspace_id: str,
    manifest_id: str,
    user_id: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
    auto_grant: bool = True,
) -> PluginInstallation:
    mf = (await db.execute(
        select(PluginManifest).where(PluginManifest.id == manifest_id)
    )).scalar_one_or_none()
    if not mf:
        raise ValueError("manifest-not-found")
    ok, err = _version_ok(mf)
    if not ok:
        raise ValueError(err or "version-incompatible")

    existing = (await db.execute(
        select(PluginInstallation).where(
            PluginInstallation.workspace_id == workspace_id,
            PluginInstallation.manifest_id == manifest_id,
        )
    )).scalar_one_or_none()
    if existing:
        existing.status = "installed"
        existing.config = config or existing.config or {}
        await db.commit()
        await load_plugin(db, existing.id)
        return existing

    inst = PluginInstallation(
        id=uuid.uuid4().hex,
        workspace_id=workspace_id, manifest_id=manifest_id,
        status="installed", installed_by=user_id,
        config=config or {},
    )
    db.add(inst)
    await db.flush()

    if auto_grant:
        for perm in (mf.permissions or []):
            db.add(PluginPermissionGrant(
                installation_id=inst.id, permission=perm,
                granted=True, granted_by=user_id,
            ))
    db.add(PluginEvent(
        installation_id=inst.id, manifest_id=mf.id,
        event="install", payload={"by": user_id},
    ))
    await db.commit()

    await load_plugin(db, inst.id)
    return inst


async def uninstall_plugin(
    db: AsyncSession, installation_id: str,
) -> None:
    inst = (await db.execute(
        select(PluginInstallation).where(PluginInstallation.id == installation_id)
    )).scalar_one_or_none()
    if not inst:
        return
    await unregister_installation(inst.id)
    db.add(PluginEvent(
        installation_id=inst.id, event="uninstall", payload={},
    ))
    await db.delete(inst)
    await db.commit()


async def set_enabled(
    db: AsyncSession, installation_id: str, *, enabled: bool,
) -> None:
    inst = (await db.execute(
        select(PluginInstallation).where(PluginInstallation.id == installation_id)
    )).scalar_one_or_none()
    if not inst:
        return
    inst.status = "installed" if enabled else "disabled"
    db.add(PluginEvent(
        installation_id=inst.id,
        event="enable" if enabled else "disable", payload={},
    ))
    await db.commit()
    if enabled:
        await load_plugin(db, inst.id)
    else:
        await unregister_installation(inst.id)


# ───────────────────────────────────────────────────────────────────────
# Load (registers hook handlers)
# ───────────────────────────────────────────────────────────────────────


async def load_plugin(
    db: AsyncSession, installation_id: str,
) -> None:
    inst = (await db.execute(
        select(PluginInstallation).where(PluginInstallation.id == installation_id)
    )).scalar_one_or_none()
    if not inst or inst.status != "installed":
        return
    mf = (await db.execute(
        select(PluginManifest).where(PluginManifest.id == inst.manifest_id)
    )).scalar_one_or_none()
    if not mf:
        return

    # Signature check (best-effort warning if missing)
    if mf.signature:
        payload = (mf.code_sha256 or mf.entrypoint).encode("utf-8")
        if not verify_against_trust_store(payload, mf.signature, mf.signed_by):
            inst.status = "error"
            inst.error_message = "signature-invalid"
            db.add(PluginEvent(installation_id=inst.id,
                               event="signature_failed", payload={}))
            await db.commit()
            return

    source = await _fetch_code(mf)
    if not source and mf.entrypoint:
        # Try local repo plugins/ dir
        cand = Path("plugins") / mf.slug / mf.entrypoint
        if cand.exists():
            source = cand.read_text(encoding="utf-8")

    granted_perms = {
        g.permission for g in (inst.grants or []) if g.granted
    }
    ctx = SDKContext(
        installation_id=inst.id,
        workspace_id=inst.workspace_id,
        plugin_slug=mf.slug,
        permissions=granted_perms,
        kv_namespace=f"{inst.workspace_id}/{mf.slug}",
    )

    # Register hook handlers
    for hook in (mf.hooks_subscribed or []):
        await register_hook(
            installation_id=inst.id, hook=hook,
            handler=_make_hook_handler(source, hook, ctx),
        )
    logger.info(
        "plugin.loaded slug=%s install=%s hooks=%s",
        mf.slug, inst.id, mf.hooks_subscribed,
    )


def _make_hook_handler(source: str, hook: str, ctx: SDKContext):
    """Build a callable that the registry invokes per event."""
    def _handler(payload: dict[str, Any]) -> Any:
        sdk_ns = make_namespace(ctx)
        result = run_plugin_code(
            source,
            entry_callable=hook,
            arg=payload,
            extra_globals={"helen_sdk": _AsModule(sdk_ns)},
        )
        if not result.ok:
            logger.warning("plugin.hook-failed install=%s hook=%s err=%s",
                           ctx.installation_id, hook, result.error)
        return {
            "ok": result.ok, "value": result.return_value,
            "stdout": result.stdout[:1024], "error": result.error,
        }
    return _handler


class _AsModule:
    """Lightweight namespace-as-module wrapper so plugin code can do
    ``from helen_sdk import send_message``."""
    def __init__(self, ns: dict[str, Any]) -> None:
        self.__dict__.update(ns)


# ───────────────────────────────────────────────────────────────────────
# Manifest registration helper
# ───────────────────────────────────────────────────────────────────────


async def register_manifest(
    db: AsyncSession, manifest_dict: dict[str, Any], *,
    code_url: Optional[str] = None,
) -> PluginManifest:
    parsed: Manifest = parse_manifest(manifest_dict)
    existing = (await db.execute(
        select(PluginManifest).where(
            PluginManifest.slug == parsed.slug,
            PluginManifest.version == parsed.version,
        )
    )).scalar_one_or_none()
    if existing:
        return existing
    row = PluginManifest(
        slug=parsed.slug, name=parsed.name, version=parsed.version,
        author=parsed.author, description=parsed.description,
        homepage=str(parsed.homepage) if parsed.homepage else None,
        min_helen_version=parsed.min_helen_version,
        max_helen_version=parsed.max_helen_version,
        permissions=list(parsed.permissions),
        entrypoint=parsed.entrypoint,
        hooks_subscribed=list(parsed.hooks_subscribed),
        ui_routes=[r.model_dump() for r in parsed.ui_routes],
        settings_schema=parsed.settings_schema,
        dependencies=list(parsed.dependencies),
        code_url=str(parsed.code_url) if parsed.code_url else code_url,
        code_sha256=parsed.code_sha256,
        signature=parsed.signature,
        signed_by=parsed.signed_by,
    )
    db.add(row)
    await db.commit()
    return row
