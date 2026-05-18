"""
LAN peer federation endpoints — list other CommClient-Server instances
discovered on the local network via UDP broadcast, and ping them over HTTP
to verify reachability.

These endpoints are intentionally unauthenticated so clients can render a
"nearby servers" list at the login screen.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, HTTPException

from app.core.config import get_settings
from app.services.discovery_service import (
    get_all_lan_ips,
    get_lan_ip,
    get_server_id,
    get_uptime_seconds,
)
from app.services.peer_registry import peer_registry

router = APIRouter(prefix="/peers", tags=["peers"])

settings = get_settings()


@router.get("")
async def list_peers(include_stale: bool = False):
    """Return the list of currently discovered peer servers on the LAN."""
    peers = await peer_registry.list(include_stale=include_stale)
    return {
        "self": {
            "server_id": get_server_id(),
            "name": settings.SERVER_NAME,
            "host": get_lan_ip(),
            "port": settings.PORT,
            "uptime": get_uptime_seconds(),
        },
        "peers": [p.to_dict() for p in peers],
        "total": len(peers),
    }


@router.get("/me")
async def me():
    """This server's own identity — useful as the other half of /peers.

    `server_code` is the 64-char alphanumeric handle clients display in
    UI (same alphabet as user share_codes). `server_id` is preserved as
    an alias for older clients that read that field name.
    """
    code = get_server_id()
    return {
        "server_code": code,
        "server_id": code,
        "name": settings.SERVER_NAME,
        "version": "1.0.0",
        "host": get_lan_ip(),
        "lan_ips": get_all_lan_ips(),
        "port": settings.PORT,
        "uptime_seconds": get_uptime_seconds(),
    }


@router.get("/{server_id}")
async def get_peer(server_id: str):
    peer = await peer_registry.get(server_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="peer not found in registry")
    return peer.to_dict()


@router.post("/{server_id}/ping")
async def ping_peer(server_id: str):
    """
    Reach out to a discovered peer via HTTP and verify it answers.
    Makes a plain GET on the peer's /api/discovery endpoint with a short
    timeout. Returns round-trip time and the peer's handshake payload so the
    caller can visually confirm the federation link is alive.
    """
    peer = await peer_registry.get(server_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="peer not found in registry")

    url = f"http://{peer.host}:{peer.port}/api/discovery"
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(url)
    except httpx.RequestError as e:
        return {
            "ok": False,
            "url": url,
            "error": str(e),
            "rtt_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    rtt_ms = round((time.perf_counter() - t0) * 1000, 2)
    payload = None
    try:
        payload = r.json()
    except Exception:
        payload = {"raw": r.text[:500]}
    return {
        "ok": r.status_code == 200,
        "url": url,
        "status_code": r.status_code,
        "rtt_ms": rtt_ms,
        "payload": payload,
    }
