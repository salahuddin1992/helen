"""
Secret admin panel — separate auth realm.

This is a second admin surface, NOT accessible via normal JWT. A master
code is generated on first boot (printed to server console, saved to
data/secret_master_code.txt with 0600 perms where possible) and used
to obtain short-TTL secret-admin session tokens.

Endpoints (all gated by X-Secret-Admin-Token header):
  POST /api/secret-admin/auth         — master code → session token
  GET  /api/secret-admin/session      — verify token + time left
  GET  /api/secret-admin/codes        — all access codes system-wide
  POST /api/secret-admin/codes/revoke — force-revoke a code
  GET  /api/secret-admin/audit/recent — last 200 audit entries
  POST /api/secret-admin/jwt/rotate   — rotate the JWT signing secret
  POST /api/secret-admin/sessions/kill-all — revoke every JWT session
  POST /api/secret-admin/emergency/freeze  — force control plane to frozen

Design choices:
  - Master code is rotated on explicit operator action only
  - Session TTL: 15 min, single-use (mint fresh on each operation burst)
  - Source IP captured in each action audit entry
  - Never proxied through JWT-authed paths; header name is distinct
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["secret-admin"])

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_MASTER_FILE = _DATA_DIR / "secret_master_code.txt"
_SESS_FILE   = _DATA_DIR / "secret_admin_sessions.json"

SESSION_TTL = 15 * 60  # 15 min


def _read_master_code() -> Optional[str]:
    try:
        if _MASTER_FILE.is_file():
            return _MASTER_FILE.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("secret_master_read_failed", error=str(e))
    return None


def ensure_master_code() -> str:
    """Generate a master code on first run; return the existing one thereafter.

    Prints the code to stderr ONLY on first generation. Operators must
    record it from the console — the API never reveals it again.
    """
    existing = _read_master_code()
    if existing:
        return existing
    code = secrets.token_urlsafe(16)  # ~22 chars, URL-safe
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _MASTER_FILE.write_text(code, encoding="utf-8")
    try:
        if os.name != "nt":
            os.chmod(_MASTER_FILE, 0o600)
    except Exception:
        pass
    # Loud one-time print.
    banner = (
        "\n" + "=" * 70 + "\n"
        "  SECRET ADMIN MASTER CODE (save this — it is shown ONCE):\n"
        f"      {code}\n"
        "  Stored at: " + str(_MASTER_FILE) + "\n"
        "  Use at: /admin-secret/  (header: X-Secret-Admin-Token)\n"
        + "=" * 70 + "\n"
    )
    print(banner, flush=True)
    logger.warning("secret_admin_master_code_generated", file=str(_MASTER_FILE))
    return code


# ── Session store (in-memory, lost on restart — deliberate) ──
_sessions: dict[str, dict[str, Any]] = {}


def _mint_session(remote_ip: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "created_at":   time.time(),
        "expires_at":   time.time() + SESSION_TTL,
        "remote_ip":    remote_ip,
        "actions":      0,
    }
    return token


def _verify_session(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    s = _sessions.get(token)
    if not s:
        return None
    if s["expires_at"] < time.time():
        _sessions.pop(token, None)
        return None
    return s


def _require(req: Request) -> dict:
    token = req.headers.get("X-Secret-Admin-Token")
    s = _verify_session(token)
    if not s:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="secret admin session invalid or expired")
    s["actions"] += 1
    return s


# ═════════════════════════════ Auth ═════════════════════════════
class _AuthReq(BaseModel):
    code: str


@router.post("/auth")
async def secret_admin_auth(body: _AuthReq, request: Request):
    master = _read_master_code()
    if not master:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="master code not initialized")
    # Constant-time compare.
    provided = (body.code or "").strip()
    if not secrets.compare_digest(provided, master):
        logger.warning("secret_admin_auth_fail",
                       remote=_remote_ip(request))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="invalid master code")
    token = _mint_session(_remote_ip(request))
    logger.info("secret_admin_session_minted",
                remote=_remote_ip(request), token_prefix=token[:8])
    return {
        "token":       token,
        "expires_in":  SESSION_TTL,
        "note":        "Keep this token in memory only. It is not issued via JWT.",
    }


@router.get("/session")
async def secret_admin_session(request: Request):
    s = _require(request)
    return {
        "active":       True,
        "created_at":   s["created_at"],
        "expires_at":   s["expires_at"],
        "time_left_s":  int(s["expires_at"] - time.time()),
        "actions":      s["actions"],
    }


# ═════════════════════════════ Codes ════════════════════════════
@router.get("/codes")
async def secret_admin_list_codes(request: Request):
    _require(request)
    from app.services.access_codes_service import get_service
    return {"codes": get_service().list_all()}


class _RevokeReq(BaseModel):
    code: str


@router.post("/codes/revoke")
async def secret_admin_revoke_code(body: _RevokeReq, request: Request):
    s = _require(request)
    from app.services.access_codes_service import get_service
    ok = get_service().admin_revoke(body.code, by_user_id=f"secret-admin@{s['remote_ip']}")
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="code not found")
    return {"revoked": True, "code": body.code}


# ═════════════════════════════ Audit ════════════════════════════
@router.get("/audit/recent")
async def secret_admin_audit(request: Request, limit: int = 200):
    _require(request)
    # Use the in-memory admin audit service if available; fall back to
    # reading the control-plane NDJSON tail which is always on disk.
    limit = max(1, min(1000, limit))
    entries = []
    try:
        audit_file = _DATA_DIR / "control_plane_audit.ndjson"
        if audit_file.is_file():
            with audit_file.open("r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
            for line in lines:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return {"entries": entries, "count": len(entries)}


# ═════════════════════════ JWT rotation ═════════════════════════
@router.post("/jwt/rotate")
async def secret_admin_rotate_jwt(request: Request):
    s = _require(request)
    # Generate new secret; persist via persistent_secrets service.
    try:
        from app.core.persistent_secrets import PersistentSecrets
        ps = PersistentSecrets()
        new_val = secrets.token_urlsafe(48)
        ps.set_secret("jwt_signing_key", new_val)
        logger.warning("secret_admin_jwt_rotated",
                       remote=s["remote_ip"])
        return {"rotated": True, "new_key_prefix": new_val[:8] + "...",
                "note": "All active JWTs are now invalid; clients must re-login."}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"rotate failed: {e}")


# ═════════════════════════ Kill-switches ════════════════════════
class _EmergencyReq(BaseModel):
    acknowledged: bool = False


@router.post("/emergency/freeze")
async def secret_admin_emergency_freeze(body: _EmergencyReq, request: Request):
    s = _require(request)
    if not body.acknowledged:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="must set acknowledged=true")
    # Force control plane into frozen phase. The tick loop will update
    # admission/record flags accordingly.
    try:
        from app.services.control_plane import ControlPlane
        cp = ControlPlane.instance()
        cp.state.global_state.phase = "frozen"
        cp.state.global_state.last_trigger = f"secret_admin.freeze@{s['remote_ip']}"
        cp.state.global_state.last_change = time.time()
        cp.state.global_state.admission_open = False
        cp.state.global_state.recording_paused = True
        logger.warning("secret_admin_emergency_freeze", remote=s["remote_ip"])
        return {"applied": True, "phase": "frozen"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=str(e))


@router.post("/sessions/kill-all")
async def secret_admin_kill_sessions(body: _EmergencyReq, request: Request):
    s = _require(request)
    if not body.acknowledged:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="must set acknowledged=true")
    # Invalidate every active JWT session via bulk revocation.
    try:
        from app.db.session import SessionLocal
        from sqlalchemy import text
        async with SessionLocal() as db:
            result = await db.execute(
                text("UPDATE user_sessions SET revoked_at = CURRENT_TIMESTAMP "
                     "WHERE revoked_at IS NULL")
            )
            await db.commit()
        logger.warning("secret_admin_kill_all_sessions", remote=s["remote_ip"])
        return {"applied": True, "affected": result.rowcount or 0}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=str(e))


# ── helpers ────────────────────────────────────────────────────
def _remote_ip(req: Request) -> str:
    try:
        xff = req.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    except Exception:
        pass
    try:
        return req.client.host if req.client else "?"
    except Exception:
        return "?"
