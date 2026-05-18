"""
Phone pairing — lets a user's phone join their Helen session as a secondary
peer (camera + mic source) without installing any app.

Flow:
  1. Desktop client hits POST /api/pair/request → gets short-lived pair_token (60s, one-shot).
  2. QR code encoded as http://<server>/pair?t=<pair_token>.
  3. Phone opens Safari on that URL → HTML page reads token from query string.
  4. Phone POSTs /api/pair/claim with pair_token → receives scoped access_token
     with extra={device_type: "phone_secondary", parent_user_id: <owner>}.
  5. Phone connects to Socket.IO with that token, joins as secondary peer.
  6. Server emits "pair:completed" to owner's sockets so desktop UI updates.

Security:
  - Pair tokens are cryptographically random, stored in-memory with TTL.
  - One-shot: claim deletes the token.
  - Issued access_token inherits owner's user_id but carries device_type marker
    so SFU routing knows it's a secondary device (its produced track becomes
    a selectable source for the owner, not a new call participant).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.core.deps import get_current_user_id
from app.core.security import create_access_token

router = APIRouter(prefix="/pair", tags=["pair"])
# Public, no /api prefix — phone types/scans this URL directly.
public_router = APIRouter(tags=["pair"])

_PAGE_PATH = Path(__file__).resolve().parent.parent.parent / "static" / "pair.html"

# ── In-memory pair-token store ─────────────────────────────
# token → { user_id, expires_at }
# LAN-only server, single-worker assumption — no Redis needed.
_PAIR_TOKENS: dict[str, dict[str, Any]] = {}
_PAIR_TTL_SECONDS = 60
_PAIR_TOKEN_MAX = 1_000  # evict oldest if exceeded
_lock = asyncio.Lock()


async def _gc_tokens() -> None:
    """Best-effort eviction of expired/overflow tokens."""
    now = time.monotonic()
    expired = [t for t, v in _PAIR_TOKENS.items() if v["expires_at"] < now]
    for t in expired:
        _PAIR_TOKENS.pop(t, None)
    while len(_PAIR_TOKENS) > _PAIR_TOKEN_MAX:
        oldest = min(_PAIR_TOKENS.items(), key=lambda kv: kv[1]["expires_at"])[0]
        _PAIR_TOKENS.pop(oldest, None)


# ── Schemas ────────────────────────────────────────────────

class PairRequestResponse(BaseModel):
    pair_token: str
    expires_in: int
    pair_url_path: str  # caller prepends scheme://host, e.g. "/pair?t=..."


class PairClaimBody(BaseModel):
    pair_token: str


class PairClaimResponse(BaseModel):
    access_token: str
    user_id: str
    device_type: str = "phone_secondary"


class PairSession(BaseModel):
    phone_sid: str
    user_id: str
    label: str
    user_agent: str
    started_at: float  # unix seconds
    duration_s: float
    claimed_by: str | None = None
    # "usb_tether" when the phone connected from Apple's 172.20.10.0/24 USB
    # hotspot subnet, otherwise "wifi". Clients use this to show a badge
    # and to prefer the USB session when multiple phones are paired.
    transport: str = "wifi"


class PairSessionsResponse(BaseModel):
    sessions: list[PairSession]


# ── Routes ─────────────────────────────────────────────────

@router.post("/request", response_model=PairRequestResponse)
async def request_pair_token(user_id: str = Depends(get_current_user_id)) -> PairRequestResponse:
    """Desktop client asks for a pair token to show as a QR code."""
    async with _lock:
        await _gc_tokens()
        token = secrets.token_urlsafe(24)  # ~32 chars, URL-safe
        _PAIR_TOKENS[token] = {
            "user_id": user_id,
            "expires_at": time.monotonic() + _PAIR_TTL_SECONDS,
        }
    return PairRequestResponse(
        pair_token=token,
        expires_in=_PAIR_TTL_SECONDS,
        pair_url_path=f"/pair?t={token}",
    )


@router.post("/claim", response_model=PairClaimResponse)
async def claim_pair_token(body: PairClaimBody) -> PairClaimResponse:
    """Phone exchanges the pair token for a scoped access token. One-shot."""
    async with _lock:
        await _gc_tokens()
        entry = _PAIR_TOKENS.pop(body.pair_token, None)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pair token invalid or expired",
        )
    if entry["expires_at"] < time.monotonic():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Pair token expired",
        )

    user_id = entry["user_id"]
    access_token = create_access_token(
        user_id=user_id,
        role="user",
        extra={"device_type": "phone_secondary", "parent_user_id": user_id},
    )

    # Notify the owner's desktop sockets (fire-and-forget — UI update only).
    try:
        from app.socket.server import emit_to_user
        await emit_to_user(
            "pair:completed",
            {"user_id": user_id, "device_type": "phone_secondary"},
            user_id,
        )
    except Exception:
        pass

    return PairClaimResponse(access_token=access_token, user_id=user_id)


@router.get("/sessions", response_model=PairSessionsResponse)
async def list_pair_sessions(
    user_id: str = Depends(get_current_user_id),
) -> PairSessionsResponse:
    """List the owner's currently-live phone pair sessions.

    Handy both for the desktop UI (show "phone connected from iPhone — 3m")
    and for auditing: the returned ``claimed_by`` field reveals which
    desktop is currently receiving media from each phone.
    """
    from app.socket.pair_handlers import list_phone_sessions

    sessions = [PairSession(**s) for s in list_phone_sessions(user_id)]
    return PairSessionsResponse(sessions=sessions)


@router.delete(
    "/sessions/{phone_sid}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def terminate_pair_session(
    phone_sid: str,
    user_id: str = Depends(get_current_user_id),
) -> Response:
    """Forcibly kick a paired phone — e.g. after the user lost the device.

    The server verifies the target phone belongs to the requesting user
    before disconnecting the socket; the normal disconnect cleanup path
    then notifies the desktop(s) and tears down the WebRTC session.
    """
    from app.socket.pair_handlers import force_disconnect_phone

    ok = await force_disconnect_phone(phone_sid, user_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Phone session not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@public_router.get("/pair", response_class=HTMLResponse)
async def pair_page(request: Request) -> HTMLResponse:
    """Serve the mobile pair HTML page. Token validated on /api/pair/claim."""
    try:
        html = _PAGE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Pair page missing")
    return HTMLResponse(content=html)
