"""
Helen-Router — Standalone Admin UI APIRouter.

Mounts the router's OWN web UI at ``/admin/``. Operators hit
``http://router.helen.lan:8080/admin/`` and reach a SPA that talks
directly to the router via the existing ``/router/*`` and ``/mesh/*``
endpoints — no Helen-Server hop.

Endpoints
---------
  GET  /admin/             → admin/index.html
  GET  /admin/login        → admin/index.html (SPA handles the screen)
  GET  /admin/vendor/{p}   → admin/vendor/{p} (chart.min.js, d3.v7.min.js)
  POST /admin/login        → {token}; if matches ROUTER_TOKEN, returns it
  POST /admin/logout       → clears server-side hint cookie
  GET  /admin/<path>       → static fallthrough from admin/

Security
--------
  - Login route is unauthenticated (operator hasn't supplied the token
    yet). It echoes the token back ONLY when the supplied value equals
    the router's secret — equivalent to "you guessed it, here's your
    session key".
  - Every other ``/admin/*`` static asset is served openly; the SPA
    enforces auth client-side and uses the token as a bearer for every
    API call. The router's LAN-only middleware already blocks WAN.
  - This module DOES NOT modify the router proxy behaviour — the
    existing ``PROXIED_PREFIXES`` tuple still ends ``/admin/`` requests
    here BEFORE the catch-all, so we register with a literal prefix
    and add a deliberate include_router in main.py BEFORE the proxy
    route is bound. The main.py edit is documented separately.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response

# Resolve the admin directory relative to this file. ``Helen-Router/
# app/admin_routes.py`` → ``Helen-Router/admin/``.
_HERE = Path(__file__).resolve().parent
_ADMIN_DIR = (_HERE.parent / "admin").resolve()
_INDEX_HTML = _ADMIN_DIR / "index.html"
_VENDOR_DIR = _ADMIN_DIR / "vendor"

# Same env var the main router uses — we reuse it as the panel's
# login token to avoid forcing operators to manage a second secret.
_ROUTER_TOKEN = os.environ.get("HELEN_ROUTER_TOKEN", "")


router = APIRouter(prefix="/admin", tags=["admin-ui"])


# ── Helpers ─────────────────────────────────────────────────────────


def _safe_join(base: Path, rel: str) -> Path | None:
    """Resolve ``base / rel`` while refusing path-traversal."""
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


# ── Routes ──────────────────────────────────────────────────────────


@router.get("/", include_in_schema=False)
async def admin_index() -> Response:
    """Serve the SPA shell."""
    if not _INDEX_HTML.is_file():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "admin/index.html missing — re-deploy the panel",
        )
    return FileResponse(
        path=str(_INDEX_HTML),
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


@router.get("/login", include_in_schema=False)
async def admin_login_page() -> Response:
    """Login is rendered by the SPA itself — same shell."""
    return await admin_index()


@router.post("/login")
async def admin_login(req: Request) -> JSONResponse:
    """Token-equality login.

    Body: ``{"token": "<hex>"}``.

    Success → ``{"ok": true, "token": "<echoed>"}``. The client stashes
    the token in ``localStorage`` and presents it as a bearer for every
    subsequent ``/router/*`` API call.
    """
    if not _ROUTER_TOKEN:
        return JSONResponse(
            {"ok": False, "error": "router_token_unset"},
            status_code=503,
        )
    try:
        body = await req.json()
    except Exception:
        body = {}
    supplied = (body or {}).get("token") or ""
    if not isinstance(supplied, str) or not supplied:
        return JSONResponse(
            {"ok": False, "error": "token_required"},
            status_code=400,
        )
    if not secrets.compare_digest(supplied, _ROUTER_TOKEN):
        return JSONResponse(
            {"ok": False, "error": "invalid_token"},
            status_code=403,
        )
    return JSONResponse(
        {"ok": True, "token": supplied,
         "issued_for": "helen-router-admin-ui"},
    )


@router.post("/logout")
async def admin_logout() -> JSONResponse:
    """Stateless logout — the client wipes localStorage. This endpoint
    exists so the SPA can mark the act in the access log."""
    return JSONResponse({"ok": True, "logged_out": True})


@router.get("/vendor/{path:path}", include_in_schema=False)
async def admin_vendor(path: str) -> Response:
    """Serve operator-placed JS libs (chart.min.js, d3.v7.min.js)."""
    target = _safe_join(_VENDOR_DIR, path)
    if target is None or not target.is_file():
        raise HTTPException(404, "vendor asset missing")
    suffix = target.suffix.lower()
    media = {
        ".js":   "application/javascript; charset=utf-8",
        ".css":  "text/css; charset=utf-8",
        ".map":  "application/json; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".svg":  "image/svg+xml",
    }.get(suffix, "application/octet-stream")
    return FileResponse(
        path=str(target),
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@router.get("/_health")
async def admin_health() -> dict[str, Any]:
    """Admin-UI liveness check (independent from /router/health)."""
    return {
        "ok": True,
        "service": "helen-router-admin-ui",
        "index_ready": _INDEX_HTML.is_file(),
        "vendor_dir_ready": _VENDOR_DIR.is_dir(),
    }


@router.get("/{path:path}", include_in_schema=False)
async def admin_static_fallthrough(path: str) -> Response:
    """Generic static serve from the admin/ directory.

    Defends against path traversal and refuses to serve dotfiles. Falls
    through to ``index.html`` for any path the SPA owns client-side
    (so deep-link reloads land back on the shell).
    """
    if not path or path.endswith("/"):
        return await admin_index()
    # Disallow hidden files / dotfiles
    if any(seg.startswith(".") for seg in path.split("/")):
        raise HTTPException(404, "not found")

    target = _safe_join(_ADMIN_DIR, path)
    if target is None:
        raise HTTPException(404, "not found")
    if target.is_file():
        suffix = target.suffix.lower()
        media = {
            ".html": "text/html; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif":  "image/gif",
            ".svg":  "image/svg+xml",
            ".ico":  "image/x-icon",
            ".txt":  "text/plain; charset=utf-8",
            ".md":   "text/markdown; charset=utf-8",
        }.get(suffix, "application/octet-stream")
        return FileResponse(path=str(target), media_type=media)
    # Path doesn't map to a real file → SPA route, return shell.
    return await admin_index()
