"""
Phase 3 / Module Q — Admin update / release endpoints.

Routes
------
GET    /api/admin/updates/manifest    — current versions across all components
POST   /api/admin/updates/check       — force update check
GET    /api/admin/updates/channels    — available channels (stable/beta/nightly)
POST   /api/admin/updates/upload      — admin uploads new binary
POST   /api/admin/updates/deploy      — roll out to clients
GET    /api/admin/updates/releases    — list known releases
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.core.audit import audit_log
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security_utils import require_role

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin/updates", tags=["admin-updates"])
settings = get_settings()

VALID_CHANNELS = ("stable", "beta", "nightly")


# ── Storage layout ──────────────────────────────────────────

def _releases_dir() -> Path:
    base = Path(settings.SQLITE_PATH).parent if Path(settings.SQLITE_PATH).is_absolute() \
        else settings.PROJECT_ROOT / "data"
    p = (base / "releases").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _release_index_file() -> Path:
    return _releases_dir() / "index.json"


def _load_index() -> dict[str, Any]:
    p = _release_index_file()
    if not p.exists():
        return {"channels": {}, "releases": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                              # pragma: no cover
        return {"channels": {}, "releases": []}


def _save_index(idx: dict[str, Any]) -> None:
    _release_index_file().write_text(
        json.dumps(idx, indent=2), encoding="utf-8",
    )


# ── Models ──────────────────────────────────────────────────

class VersionInfo(BaseModel):
    component: str
    version: Optional[str]
    sha256: Optional[str] = None
    released_at: Optional[str] = None


class ManifestOut(BaseModel):
    components: list[VersionInfo]
    channels: dict[str, dict[str, str]]


class ReleaseOut(BaseModel):
    id: str
    component: str
    channel: str
    version: str
    filename: str
    sha256: str
    size_bytes: int
    notes: Optional[str] = None
    uploaded_at: str
    deployed: bool = False
    deployed_at: Optional[str] = None


class DeployIn(BaseModel):
    release_id: str
    target_channel: str = "stable"


# ── Endpoints ───────────────────────────────────────────────

@router.get("/manifest", response_model=ManifestOut)
async def manifest(user_id: str = Depends(require_role("admin"))) -> ManifestOut:
    """Return the current version and SHA-256 of every installed component."""
    idx = _load_index()
    server_version = os.environ.get("HELEN_VERSION", "unknown")
    components: list[VersionInfo] = [
        VersionInfo(component="server", version=server_version),
        VersionInfo(component="desktop", version=idx.get("channels", {}).get("desktop_stable", {}).get("version")),
        VersionInfo(component="admin",   version=idx.get("channels", {}).get("admin_stable", {}).get("version")),
        VersionInfo(component="agent",   version=idx.get("channels", {}).get("agent_stable", {}).get("version")),
    ]
    return ManifestOut(components=components, channels=idx.get("channels", {}))


@router.post("/check")
async def force_check(user_id: str = Depends(require_role("admin"))):
    """Triggers a re-scan of the releases dir + emits a refresh signal on
    the socket. The actual update-server polling happens client-side."""
    idx = _load_index()
    audit_log("admin.updates_check", user_id=user_id, success=True)
    return {"ok": True, "releases": len(idx.get("releases", []))}


@router.get("/channels")
async def list_channels(
    user_id: str = Depends(require_role("admin")),
) -> dict[str, Any]:
    idx = _load_index()
    return {"channels": list(VALID_CHANNELS), "active": idx.get("channels", {})}


@router.post("/upload", response_model=ReleaseOut, status_code=201)
async def upload_release(
    component: str = Form(...),
    channel: str = Form("stable"),
    version: str = Form(...),
    notes: Optional[str] = Form(None),
    file: UploadFile = File(...),
    user_id: str = Depends(require_role("admin")),
) -> ReleaseOut:
    if channel not in VALID_CHANNELS:
        raise HTTPException(status_code=400, detail=f"unknown channel: {channel}")
    if component not in ("server", "desktop", "admin", "agent"):
        raise HTTPException(status_code=400, detail=f"unknown component: {component}")

    fname = f"{component}-{channel}-{version}-{file.filename}"
    target = _releases_dir() / fname
    sha = hashlib.sha256()
    size = 0
    with target.open("wb") as out:
        while chunk := await file.read(1 << 20):
            sha.update(chunk)
            out.write(chunk)
            size += len(chunk)

    rec = {
        "id": sha.hexdigest()[:24],
        "component": component, "channel": channel, "version": version,
        "filename": fname, "sha256": sha.hexdigest(),
        "size_bytes": size, "notes": notes,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "deployed": False, "deployed_at": None,
    }
    idx = _load_index()
    idx.setdefault("releases", []).append(rec)
    _save_index(idx)

    audit_log("admin.update_uploaded", user_id=user_id, success=True,
              details={"component": component, "version": version, "sha256": rec["sha256"]})
    return ReleaseOut(**rec)


@router.post("/deploy")
async def deploy_release(
    body: DeployIn,
    user_id: str = Depends(require_role("admin")),
):
    idx = _load_index()
    rec = next((r for r in idx.get("releases", []) if r["id"] == body.release_id), None)
    if not rec:
        raise HTTPException(status_code=404, detail="release not found")
    rec["deployed"] = True
    rec["deployed_at"] = datetime.now(timezone.utc).isoformat()
    channels = idx.setdefault("channels", {})
    key = f"{rec['component']}_{body.target_channel}"
    channels[key] = {
        "version": rec["version"], "sha256": rec["sha256"],
        "filename": rec["filename"], "released_at": rec["deployed_at"],
    }
    _save_index(idx)
    audit_log("admin.update_deployed", user_id=user_id, success=True,
              details={"release_id": rec["id"], "channel": body.target_channel})
    return {"ok": True, "release": rec, "channel_key": key}


@router.get("/releases", response_model=list[ReleaseOut])
async def list_releases(
    user_id: str = Depends(require_role("admin")),
) -> list[ReleaseOut]:
    idx = _load_index()
    return [ReleaseOut(**r) for r in idx.get("releases", [])]
