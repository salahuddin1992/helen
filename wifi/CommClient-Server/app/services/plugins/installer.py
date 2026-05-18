"""
Phase 7 / Module AH — Plugin Installer
======================================

Implements the 9-step install flow with progress emission, rollback,
and post-install runtime reload. Used by:

* The admin REST endpoints (``/api/admin/plugins/{slug}/install``,
  ``/upgrade``, ``/uninstall``) which delegate the heavy lifting to
  this module.
* The WebSocket fan-out via :func:`get_plugins_ws_manager`.

The nine phases (each one emits a progress event):

    1. resolve_manifest   — pull manifest from registry / upload cache
    2. download           — chunked bundle download (or use uploaded blob)
    3. verify_sha256      — re-hash and compare to manifest.code_sha256
    4. verify_signature   — Ed25519 detached signature vs trust store
    5. unpack             — extract bundle into ``data/plugins/{slug}/{version}/``
    6. preinstall         — dependency / helen-version validation, OS check
    7. copy_files         — promote unpack target to active install dir
    8. register_hooks     — call legacy ``loader.register_manifest`` +
                            create installation row + grant permissions
    9. postinstall        — fire optional ``postinstall.py`` hook
   (10. reload)           — push hooks into the in-memory registry via
                            ``loader.load_plugin``

A failure in any phase rolls back: files removed, DB rows removed,
hooks unregistered.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
import uuid
import zipfile
from dataclasses import dataclass, field
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
from app.models.plugin_job import PluginJob
from app.services.plugins.hooks import unregister_installation
from app.services.plugins.loader import (
    load_plugin,
    register_manifest,
)
from app.services.plugins.manifest_validator import ManifestValidator
from app.services.plugins.permission_review import PermissionReview
from app.services.plugins.registry_client import (
    BundleResult,
    RegistryClient,
    get_registry_client,
)
from app.services.plugins.signer import verify_against_trust_store
from app.services.plugins.ws_stream import (
    PluginsWebSocketManager,
    get_plugins_ws_manager,
)

logger = get_logger(__name__)


PLUGINS_DIR = Path(os.getenv("HELEN_PLUGINS_DIR", "data/plugins"))
PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
HELEN_VERSION = "7.0.0"


# ───────────────────────────────────────────────────────────────────────


@dataclass
class InstallContext:
    job_id: str
    slug: str
    version: str
    workspace_id: str
    actor_id: Optional[str]
    accept_permissions: bool
    accepted_codes: list[str] = field(default_factory=list)
    uploaded_bundle: Optional[bytes] = None
    uploaded_manifest: Optional[dict[str, Any]] = None

    # populated during phases
    manifest_dict: Optional[dict[str, Any]] = None
    bundle: Optional[BundleResult] = None
    install_dir: Optional[Path] = None
    installation_id: Optional[str] = None


@dataclass
class InstallResult:
    ok: bool
    job_id: str
    slug: str
    version: str
    installation_id: Optional[str]
    error: Optional[str] = None
    rolled_back: bool = False
    phases: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0


# ───────────────────────────────────────────────────────────────────────


class PluginInstaller:
    """Async install / upgrade / uninstall orchestrator."""

    PHASES: tuple[str, ...] = (
        "resolve_manifest",
        "download",
        "verify_sha256",
        "verify_signature",
        "unpack",
        "preinstall",
        "copy_files",
        "register_hooks",
        "postinstall",
        "reload",
    )

    def __init__(
        self,
        *,
        registry: Optional[RegistryClient] = None,
        ws: Optional[PluginsWebSocketManager] = None,
        review: Optional[PermissionReview] = None,
        plugins_dir: Optional[Path] = None,
    ) -> None:
        self.registry = registry or get_registry_client()
        self.ws = ws or get_plugins_ws_manager()
        self.review = review or PermissionReview()
        self.plugins_dir = plugins_dir or PLUGINS_DIR

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    async def install(
        self,
        db: AsyncSession,
        *,
        slug: str,
        version: str,
        workspace_id: str,
        actor_id: Optional[str],
        accept_permissions: bool,
        accepted_codes: Optional[list[str]] = None,
        uploaded_bundle: Optional[bytes] = None,
        uploaded_manifest: Optional[dict[str, Any]] = None,
        job_id: Optional[str] = None,
    ) -> InstallResult:
        job_id = job_id or uuid.uuid4().hex
        ctx = InstallContext(
            job_id=job_id, slug=slug, version=version,
            workspace_id=workspace_id, actor_id=actor_id,
            accept_permissions=accept_permissions,
            accepted_codes=list(accepted_codes or []),
            uploaded_bundle=uploaded_bundle,
            uploaded_manifest=uploaded_manifest,
        )
        return await self._run_phases(db, ctx, kind="install")

    async def upgrade(
        self,
        db: AsyncSession,
        *,
        slug: str,
        to_version: str,
        workspace_id: str,
        actor_id: Optional[str],
        accept_permissions: bool,
        accepted_codes: Optional[list[str]] = None,
        job_id: Optional[str] = None,
    ) -> InstallResult:
        # Same flow as install — DB upsert handles replacement.
        return await self.install(
            db, slug=slug, version=to_version,
            workspace_id=workspace_id, actor_id=actor_id,
            accept_permissions=accept_permissions,
            accepted_codes=accepted_codes,
            job_id=job_id,
        )

    async def uninstall(
        self,
        db: AsyncSession,
        *,
        slug: str,
        workspace_id: str,
        actor_id: Optional[str],
        job_id: Optional[str] = None,
    ) -> InstallResult:
        job_id = job_id or uuid.uuid4().hex
        t0 = time.perf_counter()
        job = PluginJob(
            id=job_id, slug=slug, kind="uninstall",
            actor_id=actor_id,
        )
        job.mark_running("uninstall")
        db.add(job)
        await db.commit()
        await self.ws.emit_progress(
            job_id=job_id, slug=slug, phase="uninstall", pct=10,
        )

        try:
            # Find installation
            mf = (await db.execute(
                select(PluginManifest)
                .where(PluginManifest.slug == slug)
                .order_by(PluginManifest.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            if not mf:
                raise RuntimeError("plugin-not-installed")
            inst = (await db.execute(
                select(PluginInstallation).where(
                    PluginInstallation.workspace_id == workspace_id,
                    PluginInstallation.manifest_id == mf.id,
                )
            )).scalar_one_or_none()
            if not inst:
                raise RuntimeError("plugin-not-installed")

            await unregister_installation(inst.id)
            await self.ws.emit_progress(job_id=job_id, slug=slug,
                                        phase="hooks_unregistered", pct=40)

            db.add(PluginEvent(
                installation_id=inst.id, event="uninstall",
                payload={"by": actor_id, "job_id": job_id},
            ))
            await db.delete(inst)
            await db.commit()

            # Wipe on-disk install
            plugin_dir = self.plugins_dir / slug
            if plugin_dir.exists():
                shutil.rmtree(plugin_dir, ignore_errors=True)

            await self.ws.emit_progress(job_id=job_id, slug=slug,
                                        phase="files_removed", pct=90)
            job.mark_succeeded()
            await db.commit()
            duration = int((time.perf_counter() - t0) * 1000)
            await self.ws.emit_done(
                job_id=job_id, slug=slug, ok=True, duration_ms=duration,
            )
            return InstallResult(
                ok=True, job_id=job_id, slug=slug,
                version=mf.version, installation_id=inst.id,
                duration_ms=duration,
            )
        except Exception as e:                                          # noqa: BLE001
            duration = int((time.perf_counter() - t0) * 1000)
            job.mark_failed(str(e))
            await db.commit()
            await self.ws.emit_error(
                job_id=job_id, slug=slug, phase="uninstall", error=str(e),
                fatal=True,
            )
            await self.ws.emit_done(
                job_id=job_id, slug=slug, ok=False, duration_ms=duration,
            )
            return InstallResult(
                ok=False, job_id=job_id, slug=slug,
                version="?", installation_id=None,
                error=str(e), duration_ms=duration,
            )

    # ──────────────────────────────────────────────────────────────
    # 9-step orchestration
    # ──────────────────────────────────────────────────────────────

    async def _run_phases(
        self,
        db: AsyncSession,
        ctx: InstallContext,
        *,
        kind: str,
    ) -> InstallResult:
        t0 = time.perf_counter()
        phases_log: list[dict[str, Any]] = []
        job = PluginJob(
            id=ctx.job_id, slug=ctx.slug, version=ctx.version,
            kind=kind, actor_id=ctx.actor_id,
        )
        job.mark_running(self.PHASES[0])
        db.add(job)
        await db.commit()

        try:
            for i, phase in enumerate(self.PHASES):
                t_phase = time.perf_counter()
                job.phase = phase
                job.pct = int((i / len(self.PHASES)) * 100)
                await db.commit()
                await self.ws.emit_progress(
                    job_id=ctx.job_id, slug=ctx.slug,
                    phase=phase, pct=job.pct,
                )
                method = getattr(self, f"_phase_{phase}")
                await method(db, ctx)
                phases_log.append({
                    "phase": phase, "ok": True,
                    "ms": int((time.perf_counter() - t_phase) * 1000),
                })

            job.mark_succeeded()
            await db.commit()
            duration = int((time.perf_counter() - t0) * 1000)
            await self.ws.emit_done(
                job_id=ctx.job_id, slug=ctx.slug,
                ok=True, duration_ms=duration,
                result={"installation_id": ctx.installation_id},
            )
            return InstallResult(
                ok=True, job_id=ctx.job_id, slug=ctx.slug,
                version=ctx.version,
                installation_id=ctx.installation_id,
                phases=phases_log, duration_ms=duration,
            )
        except Exception as e:                                          # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            phases_log.append({"phase": job.phase, "ok": False, "error": err})
            logger.warning("plugin.install.failed phase=%s slug=%s err=%s",
                           job.phase, ctx.slug, err)
            await self.ws.emit_error(
                job_id=ctx.job_id, slug=ctx.slug,
                phase=job.phase or "unknown", error=err, fatal=True,
            )
            rolled = await self._rollback(db, ctx)
            job.mark_failed(err)
            await db.commit()
            duration = int((time.perf_counter() - t0) * 1000)
            await self.ws.emit_done(
                job_id=ctx.job_id, slug=ctx.slug,
                ok=False, duration_ms=duration,
            )
            return InstallResult(
                ok=False, job_id=ctx.job_id, slug=ctx.slug,
                version=ctx.version,
                installation_id=ctx.installation_id,
                error=err, rolled_back=rolled,
                phases=phases_log, duration_ms=duration,
            )

    # ──────────────────────────────────────────────────────────────
    # Phase implementations
    # ──────────────────────────────────────────────────────────────

    async def _phase_resolve_manifest(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> None:
        if ctx.uploaded_manifest:
            ctx.manifest_dict = dict(ctx.uploaded_manifest)
            return
        try:
            ctx.manifest_dict = await self.registry.fetch_manifest(
                ctx.slug, ctx.version,
            )
        except Exception as e:                                          # noqa: BLE001
            raise RuntimeError(f"manifest-resolve: {e}") from e

    async def _phase_download(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> None:
        if ctx.uploaded_bundle is not None:
            ctx.bundle = self.registry.import_bundle(
                ctx.slug, ctx.version, ctx.uploaded_bundle,
                expected_sha256=(ctx.manifest_dict or {}).get("code_sha256"),
            )
            return
        manifest = ctx.manifest_dict or {}
        expected = manifest.get("code_sha256")
        signed_by = manifest.get("signed_by")

        async def _cb(done: int, total: int) -> None:
            pct = 10 + int((done / max(total or 1, 1)) * 10)
            await self.ws.emit_progress(
                job_id=ctx.job_id, slug=ctx.slug,
                phase="download", pct=min(pct, 19),
                detail={"bytes": done, "total": total},
            )

        # The registry client's progress callback is sync — wrap.
        loop = asyncio.get_running_loop()
        def progress_cb(done: int, total: int) -> None:
            try:
                loop.create_task(_cb(done, total))
            except Exception:                                           # noqa: BLE001
                pass

        ctx.bundle = await self.registry.fetch_bundle(
            ctx.slug, ctx.version,
            expected_sha256=expected, signed_by=signed_by,
            progress_cb=progress_cb,
        )

    async def _phase_verify_sha256(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> None:
        if not ctx.bundle:
            raise RuntimeError("bundle-missing")
        expected = (ctx.manifest_dict or {}).get("code_sha256")
        actual = hashlib.sha256(ctx.bundle.path.read_bytes()).hexdigest()
        if expected and actual != expected:
            raise RuntimeError(f"sha256-mismatch: {expected} != {actual}")
        ctx.bundle.sha256 = actual

    async def _phase_verify_signature(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> None:
        manifest = ctx.manifest_dict or {}
        sig = manifest.get("signature")
        if not sig:
            # Unsigned plugin — allowed but logged
            await self.ws.emit_log(
                job_id=ctx.job_id, slug=ctx.slug,
                level="warning", msg="signature-absent (community plugin)",
            )
            return
        payload = (ctx.bundle.sha256 if ctx.bundle else "").encode("utf-8")
        ok = verify_against_trust_store(
            payload, sig, manifest.get("signed_by"),
        )
        if not ok:
            raise RuntimeError("signature-invalid")

    async def _phase_unpack(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> None:
        if not ctx.bundle:
            raise RuntimeError("bundle-missing")
        target = self.plugins_dir / ctx.slug / ctx.version / ".staging"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(ctx.bundle.path, "r") as zf:
                for n in zf.namelist():
                    if os.path.isabs(n) or ".." in Path(n).parts:
                        raise RuntimeError(f"zip-slip: {n}")
                zf.extractall(target)
        except zipfile.BadZipFile:
            # Treat as single-file plugin (legacy)
            (target / "plugin.py").write_bytes(ctx.bundle.path.read_bytes())
        ctx.install_dir = target

    async def _phase_preinstall(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> None:
        manifest = ctx.manifest_dict or {}
        installed_slugs = {
            s for (s,) in (await db.execute(select(PluginManifest.slug))).all()
        }
        v = ManifestValidator(installed_slugs=installed_slugs)
        res = v.validate(manifest)
        if not res.ok:
            raise RuntimeError(
                "manifest-invalid: " + "; ".join(res.errors)
                + (f"; missing_deps={res.missing_dependencies}"
                   if res.missing_dependencies else "")
            )
        # Permission gate (high/critical perms require explicit accept)
        allowed, why, missing = self.review.gate_install(
            manifest.get("permissions") or [],
            accepted=ctx.accept_permissions,
            explicitly_accepted=ctx.accepted_codes,
        )
        if not allowed:
            raise RuntimeError(
                f"permission-gate: {why} missing={missing}"
            )

        # Optional preinstall.py — must exit 0
        if ctx.install_dir:
            pre = ctx.install_dir / "preinstall.py"
            if pre.exists():
                self._run_script(pre, ctx, phase="preinstall")

    async def _phase_copy_files(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> None:
        if not ctx.install_dir:
            raise RuntimeError("staging-missing")
        active = self.plugins_dir / ctx.slug / ctx.version
        if active.exists():
            for p in active.iterdir():
                if p.name == ".staging":
                    continue
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
        for entry in ctx.install_dir.iterdir():
            dest = active / entry.name
            if entry.is_dir():
                shutil.copytree(entry, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, dest)
        shutil.rmtree(ctx.install_dir, ignore_errors=True)
        ctx.install_dir = active

    async def _phase_register_hooks(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> None:
        manifest_dict = dict(ctx.manifest_dict or {})
        # Point code_url at the on-disk file://
        if ctx.install_dir:
            entry = manifest_dict.get("entrypoint") or "plugin.py"
            file_path = (ctx.install_dir / entry).resolve()
            manifest_dict["code_url"] = f"file://{file_path.as_posix()}"

        mf = await register_manifest(
            db, manifest_dict, code_url=manifest_dict.get("code_url"),
        )

        # Upsert PluginInstallation
        inst = (await db.execute(
            select(PluginInstallation).where(
                PluginInstallation.workspace_id == ctx.workspace_id,
                PluginInstallation.manifest_id == mf.id,
            )
        )).scalar_one_or_none()
        if inst is None:
            inst = PluginInstallation(
                workspace_id=ctx.workspace_id,
                manifest_id=mf.id, status="installed",
                installed_by=ctx.actor_id, config={},
            )
            db.add(inst)
            await db.flush()
        else:
            inst.status = "installed"
            inst.installed_by = ctx.actor_id
        # Grant accepted permissions
        for perm in (mf.permissions or []):
            existing = next(
                (g for g in (inst.grants or [])
                 if g.permission == perm), None,
            )
            if existing:
                existing.granted = True
            else:
                db.add(PluginPermissionGrant(
                    installation_id=inst.id, permission=perm,
                    granted=True, granted_by=ctx.actor_id,
                ))
        db.add(PluginEvent(
            installation_id=inst.id, manifest_id=mf.id,
            event="install",
            payload={"by": ctx.actor_id, "job_id": ctx.job_id,
                     "version": ctx.version},
        ))
        await db.commit()
        ctx.installation_id = inst.id

    async def _phase_postinstall(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> None:
        if not ctx.install_dir:
            return
        post = ctx.install_dir / "postinstall.py"
        if post.exists():
            self._run_script(post, ctx, phase="postinstall")

    async def _phase_reload(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> None:
        if not ctx.installation_id:
            return
        try:
            await load_plugin(db, ctx.installation_id)
        except Exception as e:                                          # noqa: BLE001
            # Non-fatal — surface as warning, don't roll back the install.
            await self.ws.emit_log(
                job_id=ctx.job_id, slug=ctx.slug,
                level="warning", msg=f"reload-warning: {e}",
            )

    # ──────────────────────────────────────────────────────────────
    # Rollback
    # ──────────────────────────────────────────────────────────────

    async def _rollback(
        self, db: AsyncSession, ctx: InstallContext,
    ) -> bool:
        try:
            # Drop staging dir
            if ctx.install_dir and ctx.install_dir.exists():
                staging = ctx.install_dir if ctx.install_dir.name == ".staging" \
                          else ctx.install_dir.parent / ".staging"
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            # Drop installation if it was created
            if ctx.installation_id:
                inst = (await db.execute(
                    select(PluginInstallation).where(
                        PluginInstallation.id == ctx.installation_id
                    )
                )).scalar_one_or_none()
                if inst:
                    await unregister_installation(inst.id)
                    await db.delete(inst)
                    await db.commit()
            return True
        except Exception as e:                                          # noqa: BLE001
            logger.warning("plugin.install.rollback-failed: %s", e)
            return False

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────

    def _run_script(
        self, script: Path, ctx: InstallContext, *, phase: str,
    ) -> None:
        """Run a pre/post-install script with a hard timeout. Exit != 0 raises."""
        py = shutil.which("python") or shutil.which("python3") or "python"
        cmd = [py, str(script)]
        try:
            proc = subprocess.run(                                       # noqa: S603
                cmd, cwd=str(script.parent),
                capture_output=True, text=True, timeout=30,
                env={**os.environ,
                     "HELEN_PLUGIN_SLUG": ctx.slug,
                     "HELEN_PLUGIN_VERSION": ctx.version,
                     "HELEN_PLUGIN_PHASE": phase},
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if platform.system() == "Windows" else 0
                ),
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"{phase}-timeout") from e
        if proc.returncode != 0:
            raise RuntimeError(
                f"{phase}-failed code={proc.returncode} "
                f"stderr={proc.stderr[:512]}"
            )


_default_installer: Optional[PluginInstaller] = None


def get_installer() -> PluginInstaller:
    global _default_installer
    if _default_installer is None:
        _default_installer = PluginInstaller()
    return _default_installer


__all__ = [
    "PluginInstaller", "InstallResult", "InstallContext",
    "get_installer", "PLUGINS_DIR",
]
