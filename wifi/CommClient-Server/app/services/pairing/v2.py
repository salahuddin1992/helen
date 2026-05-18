"""
Phase 3 / Module O — Mobile pairing v2.

Improvements over v1 (``app.api.routes.pair``):

  * Larger QR payload that bundles every reachable URL (LAN + WAN tunnel)
    so the phone can pick the best route automatically (mDNS-friendly).
  * Numeric 6-digit code path for the cases where the phone can't scan
    the QR (locked / dirty camera) — typed in by hand on a secondary
    pairing screen.
  * Rate-limited issuance: max 5 codes per user per hour.
  * Status polling channel so the desktop UI can show "phone connected"
    without waiting on a socket event.
  * Pair → returns BOTH an access AND a refresh token (real device
    enrollment, not the v1 single-shot secondary-device token).

This module is pure logic. The router lives in
``app.api.routes.pairing_v2``.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.core.security import create_access_token, create_refresh_token

logger = get_logger(__name__)


# ── Configuration ──────────────────────────────────────────

DEFAULT_TTL_SECONDS = 300
MAX_ACTIVE_CODES = 1_000
MAX_PER_USER_PER_HOUR = 5
COMPLETED_TTL_SECONDS = 600


# ── Models ──────────────────────────────────────────────────

@dataclass
class PairingEntry:
    code: str
    nonce: str
    user_id: str
    created_at: float
    expires_at: float
    status: str = "pending"        # pending | completed | expired | revoked
    device_id: Optional[str] = None
    device_info: dict[str, Any] = field(default_factory=dict)
    completed_at: Optional[float] = None


# ── In-memory store ────────────────────────────────────────

_STORE: dict[str, PairingEntry] = {}
_RECENT_PER_USER: dict[str, list[float]] = {}
_LOCK = asyncio.Lock()


def _now() -> float:
    return time.monotonic()


def _wall_now() -> float:
    return time.time()


def _gen_code() -> str:
    # 6-digit zero-padded; rejects easily-confused leading-zero codes by
    # using full 0..999_999 range. Phone UIs render with monospace.
    return f"{secrets.randbelow(1_000_000):06d}"


def _gen_nonce() -> str:
    return secrets.token_urlsafe(24)


async def _evict_expired() -> None:
    now = _now()
    dead = [
        c for c, e in _STORE.items()
        if (e.status == "pending" and e.expires_at < now)
        or (e.status in ("completed", "revoked", "expired")
            and (now - (e.completed_at or e.created_at)) > COMPLETED_TTL_SECONDS)
    ]
    for c in dead:
        e = _STORE.get(c)
        if e and e.status == "pending":
            e.status = "expired"
        else:
            _STORE.pop(c, None)

    if len(_STORE) > MAX_ACTIVE_CODES:
        # FIFO eviction of oldest pending codes.
        ordered = sorted(_STORE.items(), key=lambda kv: kv[1].created_at)
        for c, _ in ordered[: max(0, len(_STORE) - MAX_ACTIVE_CODES)]:
            _STORE.pop(c, None)


def _can_issue(user_id: str) -> bool:
    """Rate limit: MAX_PER_USER_PER_HOUR pending codes per user."""
    now = _wall_now()
    window = now - 3600
    bucket = _RECENT_PER_USER.setdefault(user_id, [])
    bucket[:] = [t for t in bucket if t >= window]
    return len(bucket) < MAX_PER_USER_PER_HOUR


# ── Public API ─────────────────────────────────────────────

async def generate_pairing_code(
    user_id: str,
    *,
    ttl: int = DEFAULT_TTL_SECONDS,
    server_url: str,
    lan_urls: Optional[list[str]] = None,
    wan_tunnel_id: Optional[str] = None,
) -> dict[str, Any]:
    """Allocate a fresh pairing code + nonce + QR payload."""
    async with _LOCK:
        await _evict_expired()
        if not _can_issue(user_id):
            raise PermissionError(
                "Pairing rate limit reached — too many active codes in the last hour.",
            )

        # Avoid 6-digit collisions; retry up to a few times.
        for _ in range(5):
            code = _gen_code()
            if code not in _STORE:
                break
        else:                                                      # pragma: no cover
            raise RuntimeError("Could not allocate a unique pairing code.")

        nonce = _gen_nonce()
        entry = PairingEntry(
            code=code, nonce=nonce, user_id=user_id,
            created_at=_now(),
            expires_at=_now() + ttl,
        )
        _STORE[code] = entry
        _RECENT_PER_USER.setdefault(user_id, []).append(_wall_now())

    expires_in = int(entry.expires_at - _now())
    payload: dict[str, Any] = {
        "code": code,
        "nonce": nonce,
        "expires_at_unix": int(_wall_now() + expires_in),
        "server_url": server_url,
        "lan_urls": lan_urls or [],
        "wan_tunnel_id": wan_tunnel_id,
        "version": 2,
    }
    qr_payload = "helen:pair?" + json_safe_encode(payload)

    audit_log("pairing.v2_code_issued", user_id=user_id, success=True,
              details={"code": code, "ttl": ttl})
    return {
        "code": code,
        "nonce": nonce,
        "expires_in": expires_in,
        "qr_payload": qr_payload,
        "json_payload": payload,
    }


def json_safe_encode(d: dict[str, Any]) -> str:
    """URL-safe compact JSON encoding for the QR payload."""
    import urllib.parse
    return urllib.parse.quote(json.dumps(d, separators=(",", ":")), safe=":,{}[]\"=")


async def verify_and_pair(
    code: str,
    nonce: str,
    device_info: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Mobile-side: redeem the code + nonce, mint JWT tokens, mark complete."""
    async with _LOCK:
        await _evict_expired()
        entry = _STORE.get(code)
        if not entry:
            raise ValueError("invalid_or_expired_code")
        if entry.status != "pending":
            raise ValueError(f"code_already_{entry.status}")
        if not secrets.compare_digest(entry.nonce, nonce):
            raise ValueError("nonce_mismatch")
        if entry.expires_at < _now():
            entry.status = "expired"
            raise ValueError("invalid_or_expired_code")

        device_id = "dev_" + secrets.token_hex(8)
        entry.status = "completed"
        entry.completed_at = _now()
        entry.device_id = device_id
        entry.device_info = dict(device_info or {})

    access = create_access_token(
        entry.user_id, role="user",
        extra={"device_id": device_id, "device_type": "phone_v2"},
    )
    refresh = create_refresh_token(entry.user_id)

    audit_log("pairing.v2_completed", user_id=entry.user_id, success=True,
              details={"code": code, "device_id": device_id,
                       "platform": entry.device_info.get("platform")})

    return {
        "access_token": access,
        "refresh_token": refresh,
        "user_id": entry.user_id,
        "device_id": device_id,
        "token_type": "bearer",
    }


async def get_status(code: str) -> dict[str, Any]:
    async with _LOCK:
        await _evict_expired()
        entry = _STORE.get(code)
        if not entry:
            return {"status": "expired", "code": code}
        return {
            "code": code,
            "status": entry.status,
            "expires_in": max(0, int(entry.expires_at - _now())),
            "device_id": entry.device_id,
            "device_info": entry.device_info,
        }


async def revoke_code(code: str, requester_user_id: str) -> bool:
    async with _LOCK:
        entry = _STORE.get(code)
        if not entry or entry.user_id != requester_user_id:
            return False
        entry.status = "revoked"
        entry.completed_at = _now()
    return True
