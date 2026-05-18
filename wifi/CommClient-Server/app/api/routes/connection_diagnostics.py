"""
Connection diagnostics endpoint — structured per-client connection state.

Replaces the generic "disconnected" UI state with explicit signals:
  serverReachable, authValid, deviceRegistered, websocketConnected,
  heartbeatHealthy. Consumed by the desktop client's diagnostics panel
  (Phase 6) and by scripts/diagnose-connection.ps1 (Phase 8).

No new database tables — derives everything from existing services
(presence, sessions, JWT decode).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_db
from app.core.security import decode_token_no_http
from app.models import User, UserSession
from app.services import federation_resilience
from app.services.discovery_service import (
    get_all_lan_ips,
    get_lan_ip,
    get_server_id,
    get_uptime_seconds,
)
from app.services.presence_service import presence_service

router = APIRouter(prefix="/connection", tags=["system"])
settings = get_settings()


def _bearer(token_header: str | None) -> str | None:
    if not token_header:
        return None
    parts = token_header.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return token_header.strip() or None


@router.get("/diagnostics")
async def connection_diagnostics(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """
    Returns a structured connection state for the calling client.

    Unauthenticated callers get serverReachable + serverInfo only.
    Authenticated callers also get authValid, userOnline, sessionCount,
    socketCount.

    Response shape:
      {
        "serverReachable": true,
        "serverInfo": {...},
        "authValid": true|false|null,
        "authError": null | "InvalidToken" | "TokenExpired" | "UserNotFoundOnServer",
        "user": {"id": "...", "username": "..."} | null,
        "userOnline": true|false|null,
        "sessionCount": int|null,
        "socketCount": int|null,
        "timestamp": <unix ts>
      }
    """
    online_users = await presence_service.get_online_user_ids()
    server_info = {
        "service": settings.SERVER_NAME,
        "server_id": get_server_id(),
        "lan_ip": get_lan_ip(),
        "lan_ips": get_all_lan_ips(),
        "port": settings.PORT,
        "uptime_seconds": get_uptime_seconds(),
        "online_users": len(online_users),
        "client_ip": request.client.host if request.client else None,
    }

    out: dict[str, Any] = {
        "serverReachable": True,
        "serverInfo": server_info,
        "authValid": None,
        "authError": None,
        "user": None,
        "userOnline": None,
        "sessionCount": None,
        "socketCount": None,
        "timestamp": int(time.time()),
        # Per-peer circuit breaker visibility — empty when no federation
        # peers exist or no failures have been recorded. Useful for
        # diagnosing "messages not arriving cross-server" without tailing
        # logs on every peer.
        "federation": {
            "breakers": await federation_resilience.breaker_snapshot(),
        },
    }

    token = _bearer(authorization)
    if not token:
        return out

    payload = decode_token_no_http(token)
    if not payload or payload.get("type") != "access":
        out["authValid"] = False
        out["authError"] = "InvalidToken"
        return out

    user_id = payload.get("sub")
    if not user_id:
        out["authValid"] = False
        out["authError"] = "InvalidToken"
        return out

    user_row = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user_row:
        # Token signature is valid but the user doesn't exist on THIS server.
        # That means the token was issued by a different server — split-brain.
        # The client should clear its credentials and re-login here.
        out["authValid"] = False
        out["authError"] = "UserNotFoundOnServer"
        return out

    out["authValid"] = True
    out["user"] = {
        "id": user_row.id,
        "username": user_row.username,
        "role": user_row.role,
    }
    out["userOnline"] = user_id in online_users

    socket_ids = await presence_service.get_socket_ids(user_id)
    out["socketCount"] = len(socket_ids)

    sessions = (
        await db.execute(
            select(UserSession).where(UserSession.user_id == user_id)
        )
    ).scalars().all()
    out["sessionCount"] = len(sessions)

    return out
