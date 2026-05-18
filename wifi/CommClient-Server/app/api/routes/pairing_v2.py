"""
Phase 3 / Module O — Pairing v2 REST endpoints.

Mounted under the ``/api/pairing/v2`` prefix by
``app.api.routes._phase3_routers.register_phase3_routers``.

Routes
------
POST /api/pairing/v2/start            (auth required)
POST /api/pairing/v2/complete         (anonymous — used by phone)
GET  /api/pairing/v2/qr/{code}        (PNG image of QR payload)
GET  /api/pairing/v2/status/{code}    (desktop poll endpoint)
POST /api/pairing/v2/revoke/{code}    (auth required — owner only)
"""
from __future__ import annotations

import io
import socket
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.deps import get_current_user_id
from app.core.logging import get_logger
from app.services.pairing import v2 as pairing_v2

logger = get_logger(__name__)

router = APIRouter(prefix="/api/pairing/v2", tags=["pairing-v2"])
settings = get_settings()


# ── helpers ────────────────────────────────────────────────

def _lan_urls(request: Request) -> list[str]:
    """Best-effort enumeration of reachable URLs for this server.
    The phone picks the first one that connects within ~250 ms."""
    out: list[str] = []
    scheme = "https" if settings.HTTPS_ENABLED else "http"

    # Primary: whatever the caller used.
    host_header = request.headers.get("host")
    if host_header:
        out.append(f"{scheme}://{host_header}")

    # Add every local IPv4 we can find.
    seen: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ":" in ip:
                continue  # skip IPv6 for QR brevity
            if ip in seen or ip.startswith(("169.254.", "127.")):
                continue
            seen.add(ip)
            out.append(f"{scheme}://{ip}:{settings.PORT}")
    except Exception:                                              # pragma: no cover
        pass
    return out[:6]


# ── Shapes ─────────────────────────────────────────────────

class StartIn(BaseModel):
    ttl_seconds: int = pairing_v2.DEFAULT_TTL_SECONDS
    wan_tunnel_id: Optional[str] = None


class StartOut(BaseModel):
    code: str
    nonce: str
    expires_in: int
    qr_payload: str
    json_payload: dict[str, Any]


class CompleteIn(BaseModel):
    code: str
    nonce: str
    device_info: Optional[dict[str, Any]] = None


class CompleteOut(BaseModel):
    access_token: str
    refresh_token: str
    user_id: str
    device_id: str
    token_type: str = "bearer"


class StatusOut(BaseModel):
    code: str
    status: str
    expires_in: int = 0
    device_id: Optional[str] = None
    device_info: dict[str, Any] = {}


# ── Routes ─────────────────────────────────────────────────

@router.post("/start", response_model=StartOut)
async def start_pairing(
    body: StartIn,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> StartOut:
    try:
        server_base = (_lan_urls(request) or ["http://localhost:3000"])[0]
        payload = await pairing_v2.generate_pairing_code(
            user_id,
            ttl=max(60, min(body.ttl_seconds, 1800)),
            server_url=server_base,
            lan_urls=_lan_urls(request),
            wan_tunnel_id=body.wan_tunnel_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    return StartOut(**payload)


@router.post("/complete", response_model=CompleteOut)
async def complete_pairing(body: CompleteIn) -> CompleteOut:
    try:
        tok = await pairing_v2.verify_and_pair(
            body.code, body.nonce, body.device_info,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return CompleteOut(**tok)


@router.get("/qr/{code}")
async def qr_image(code: str):
    """Generate the QR image for an already-issued pairing code. We don't
    re-issue here — caller must have called ``/start`` first."""
    info = await pairing_v2.get_status(code)
    if info.get("status") not in ("pending",):
        raise HTTPException(status_code=410, detail="code not active")
    # The QR encodes the helen: payload built at /start. We don't store
    # the QR payload separately, so we encode whatever the phone needs
    # to redeem: the code itself (the phone reads server_url from the
    # context page). That said, prefer importing `qrcode` if available.
    try:
        import qrcode  # type: ignore
    except Exception:
        raise HTTPException(
            status_code=501,
            detail="QR rendering unavailable — install qrcode[pil].",
        )

    img = qrcode.make(f"helen:pair?code={code}")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.get("/status/{code}", response_model=StatusOut)
async def status_for_code(code: str) -> StatusOut:
    info = await pairing_v2.get_status(code)
    return StatusOut(
        code=info.get("code", code),
        status=info.get("status", "expired"),
        expires_in=info.get("expires_in", 0),
        device_id=info.get("device_id"),
        device_info=info.get("device_info", {}),
    )


@router.post("/revoke/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_pairing_code(
    code: str, user_id: str = Depends(get_current_user_id),
) -> None:
    ok = await pairing_v2.revoke_code(code, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="code not found")
