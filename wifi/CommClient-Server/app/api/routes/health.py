"""
Health and system info endpoints.

/health     — lightweight liveness check (no auth, <1ms)
/info       — full server metadata (no auth, for discovery verification)
/discovery  — minimal discovery handshake endpoint (no auth)
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from app.core.config import get_settings
from app.services.discovery_service import (
    get_all_lan_ips,
    get_lan_ip,
    get_server_id,
    get_uptime_seconds,
)
from app.services.presence_service import presence_service

router = APIRouter(tags=["system"])

settings = get_settings()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": settings.SERVER_NAME,
        "version": "1.0.0",
    }


@router.get("/info")
async def info():
    online = await presence_service.get_all_online()
    return {
        "service": settings.SERVER_NAME,
        "version": "1.0.0",
        "server_id": get_server_id(),
        "name": settings.SERVER_NAME,
        "lan_ip": get_lan_ip(),
        "lan_ips": get_all_lan_ips(),
        "port": settings.PORT,
        "uptime_seconds": get_uptime_seconds(),
        "online_users": len(online),
    }


@router.get("/uplink")
async def uplink():
    """Report whether this server is currently linked to a parent
    (rendezvous) server.

    The desktop client polls this on the HealthCheck panel so the
    operator can see at a glance whether the server has a working
    public-internet uplink — i.e. whether external clients can reach
    it via the rendezvous tunnel.

    No authentication required: the response is intentionally
    sanitized — it never leaks the rendezvous token, only the URL and
    a connected flag. Useful for an unauthenticated splash check.
    """
    try:
        from app.services.connectivity import orchestrator as _conn
        full = _conn.status()
    except Exception:
        return {"connected": False, "method": None, "configured": False}

    tunnel = (full.get("strategies") or {}).get("reverse_tunnel") or {}
    return {
        "connected":  bool(tunnel.get("connected")),
        "configured": bool(tunnel.get("configured")),
        "method":     "reverse_tunnel" if tunnel.get("connected") else None,
        # Public-facing URL only — token is never echoed.
        "ws_url":         tunnel.get("ws_url") or None,
        "public_id":      tunnel.get("public_id") or None,
        "active_methods": list(full.get("active_methods") or []),
    }


@router.get("/discovery")
async def discovery():
    """
    Lightweight discovery handshake endpoint.
    Clients call this to verify a discovered server is real and reachable.
    Returns the minimum data needed for the client to connect.
    No authentication required.
    """
    import os as _os
    online = await presence_service.get_all_online()
    lan_ip = get_lan_ip()
    https_enabled = not _os.environ.get("HELEN_HTTPS_DISABLED", "").lower() in {"1", "true", "yes", "on"}
    https_port = int(_os.environ.get("HELEN_HTTPS_PORT", "3443"))
    out = {
        "type": "commclient-server",
        "server_id": get_server_id(),
        "name": settings.SERVER_NAME,
        "version": "1.0.0",
        "host": lan_ip,
        "port": settings.PORT,
        "users_online": len(online),
        "uptime": get_uptime_seconds(),
        "ts": int(time.time()),
    }
    if https_enabled:
        # Advertise the HTTPS URL so mobile browsers can pair (Safari/
        # Android Chrome refuse getUserMedia on plain http://LAN-IP).
        out["https_port"] = https_port
        out["https_url"] = f"https://{lan_ip}:{https_port}"
        out["pair_url_https"] = f"https://{lan_ip}:{https_port}/pair"
    return out
