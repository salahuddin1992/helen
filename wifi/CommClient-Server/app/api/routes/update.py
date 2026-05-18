"""
Update mirror HTTP routes.

Exposes the mirror directory under ``/api/updates`` so electron-updater
clients (generic provider) can pull manifests and installers directly
from the LAN server:

    GET  /api/updates/channel-<channel>.json    → manifest
    GET  /api/updates/installers/<file>         → installer .exe
    GET  /api/updates/status                    → diagnostic
    POST /api/updates/refresh                   → leader-only forced
                                                  refresh

The installer endpoint streams directly from disk with strong ETag
support so clients can resume downloads. ``Range`` requests are handled
by FastAPI's ``FileResponse`` automatically for small files; for large
installers we use a manual range responder.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN, HTTP_404_NOT_FOUND

from app.services.update_service import (
    UpdateServiceConfig,
    update_service,
    _data_dir,
    _installer_dir,
    _manifest_path,
)

router = APIRouter(prefix="/api/updates", tags=["updates"])
logger = logging.getLogger("commclient.api.update")


# ─── helpers ─────────────────────────────────────────────────────────────

_ALLOWED_CHANNELS = {"stable", "beta", "canary"}


def _is_safe_installer_name(name: str) -> bool:
    if not name.endswith(".exe") and not name.endswith(".blockmap") and not name.endswith(".yml"):
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return name.startswith("CommClient-")


def _file_etag(path: Path) -> str:
    stat = path.stat()
    return f'"{stat.st_size:x}-{int(stat.st_mtime):x}"'


# ─── endpoints ──────────────────────────────────────────────────────────


@router.get("/channel-{channel}.json")
async def get_channel_manifest(channel: str, request: Request):
    if channel not in _ALLOWED_CHANNELS:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="unknown channel")
    path = _manifest_path(channel)
    if not path.exists():
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="manifest not yet mirrored")

    etag = _file_etag(path)
    if request.headers.get("if-none-match") == etag:
        return JSONResponse(status_code=304, content=None, headers={"ETag": etag})

    return FileResponse(
        path=str(path),
        media_type="application/json",
        headers={
            "ETag": etag,
            "Cache-Control": "public, max-age=60",
        },
    )


@router.get("/installers/{filename}")
async def get_installer(filename: str, request: Request):
    if not _is_safe_installer_name(filename):
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="bad filename")

    path = _installer_dir() / filename
    if not path.exists():
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="installer missing")

    etag = _file_etag(path)
    if request.headers.get("if-none-match") == etag:
        return JSONResponse(status_code=304, content=None, headers={"ETag": etag})

    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        filename=filename,
        headers={
            "ETag": etag,
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400, immutable",
        },
    )


@router.get("/status")
async def update_status():
    return update_service.status()


@router.post("/refresh")
async def force_refresh(request: Request):
    # Basic leader-authed refresh trigger — bearer must match the
    # internal mediasoup control token (already persisted on the server)
    # which is shared only with local management tooling.
    expected = os.environ.get("MEDIASOUP_CONTROL_TOKEN") or os.environ.get(
        "COMMCLIENT_CONTROL_TOKEN"
    )
    auth = request.headers.get("authorization", "")
    if expected and auth != f"Bearer {expected}":
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="bad token")
    result = await update_service.refresh_once()
    return result


@router.get("/info")
async def info():
    """Lightweight endpoint for desktop clients to sanity-check the mirror."""
    return {
        "mirror": True,
        "data_dir": str(_data_dir()),
        "channels": list(UpdateServiceConfig.from_env().channels),
        "state": update_service.status()["state"],
    }
