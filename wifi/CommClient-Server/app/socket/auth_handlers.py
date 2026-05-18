"""
Live Socket.IO auth events.

Why this module exists
----------------------
The connect handler in `app/socket/server.py` validates the JWT once at
TCP-level Socket.IO connection. Once accepted, the socket has no
mechanism to re-authenticate when the access token approaches expiry —
so a long-running call (>JWT_ACCESS_TOKEN_EXPIRE_MINUTES) ends up with
a "live" socket whose user can't make REST calls anymore (every
authenticated REST endpoint will 401).

`auth:refresh` lets a client trade a refresh_token for a new
access_token mid-session. The new token is decoded, validated, and
the socket's session is patched in place — no reconnect required.
The client then uses the new token for HTTP calls; the socket itself
keeps running under its existing connection identity.
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    decode_token_no_http,
)
from app.socket.server import get_user_id, sio

logger = get_logger(__name__)


@sio.event
async def auth_refresh(sid: str, data: dict):
    """Refresh the access token bound to this socket.

    Payload: ``{ "refresh_token": "..." }``

    Returns:
      * ``{ "ok": True, "access_token": "...", "expires_in": <seconds> }``
        on success
      * ``{ "ok": False, "error": "..." }`` on failure (the client
        should re-login by reconnecting with a fresh access token)

    The refresh token's ``sub`` MUST match the current socket's user_id
    — we never re-issue tokens for a different user mid-socket.
    """
    current_user = await get_user_id(sid)
    if not current_user:
        return {"ok": False, "error": "no_session"}

    if not isinstance(data, dict):
        return {"ok": False, "error": "bad_payload"}

    token = data.get("refresh_token")
    if not isinstance(token, str) or not token or len(token) > 4096:
        return {"ok": False, "error": "bad_token"}

    payload = decode_token_no_http(token)
    if not payload or payload.get("type") != "refresh":
        logger.warning("auth_refresh_invalid_token", sid=sid, user_id=current_user)
        return {"ok": False, "error": "invalid_or_expired_refresh"}

    sub = payload.get("sub")
    if sub != current_user:
        # Refresh tokens are bound to a user. A different sub is either
        # a misconfigured client OR a token-swap attack. Refuse.
        logger.warning(
            "auth_refresh_user_mismatch",
            sid=sid, current_user=current_user, refresh_sub=sub,
        )
        return {"ok": False, "error": "user_mismatch"}

    # Mint a fresh access token. We preserve the role from the existing
    # session so the new token has the same RBAC as the original auth.
    role = "user"
    try:
        async with sio.session(sid) as session:
            role = session.get("role") or role
    except Exception:
        pass

    try:
        new_access = create_access_token(user_id=current_user, role=role)
    except Exception as e:
        logger.error("auth_refresh_mint_failed",
                     sid=sid, user_id=current_user, error=str(e))
        return {"ok": False, "error": "mint_failed"}

    # Decode just to read the exp/iat — saves a settings lookup race.
    new_payload = decode_token_no_http(new_access)
    expires_in = None
    if new_payload and "exp" in new_payload and "iat" in new_payload:
        try:
            expires_in = int(new_payload["exp"]) - int(new_payload["iat"])
        except Exception:
            expires_in = None

    # Touch the socket session — record the latest token's exp so the
    # disconnect path / observability can correlate.
    try:
        async with sio.session(sid) as session:
            if new_payload and "exp" in new_payload:
                session["token_exp"] = int(new_payload["exp"])
    except Exception:
        pass

    logger.info("auth_refresh_ok",
                sid=sid, user_id=current_user, expires_in=expires_in)
    return {"ok": True, "access_token": new_access, "expires_in": expires_in}
