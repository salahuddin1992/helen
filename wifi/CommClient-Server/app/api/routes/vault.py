"""
Helen-Vault — a dedicated realm for every secret code in the system.

Scope:
    - user share codes (with avatar + display_name)
    - user-minted access codes (from access_codes_service)
    - secret-admin master code metadata (fingerprint only, never the plaintext)
    - active secret-admin sessions

This is a separate auth realm from the regular admin: its own master
code at data/vault_master_code.txt generated on first boot, a
dedicated session token carried via X-Vault-Token header, and a
LAN-only dependency that refuses any request whose source IP is not
in a private RFC1918 range or loopback.

Route prefix: /api/vault/*
Static UI: /vault/ (mounted in app/main.py, pointing at /Vault/web/)
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["vault"])

# ── Paths ────────────────────────────────────────────────────────
_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                                str(Path(__file__).resolve().parents[3] / "data")))
_MASTER_FILE = _DATA_DIR / "vault_master_code.txt"

SESSION_TTL = 30 * 60  # 30 minutes

# ── Master code lifecycle ────────────────────────────────────────
def ensure_master_code() -> str:
    """Generate once on first call; read thereafter. Prints loudly once."""
    if _MASTER_FILE.is_file():
        try:
            v = _MASTER_FILE.read_text(encoding="utf-8").strip()
            if v: return v
        except Exception:
            pass
    code = secrets.token_urlsafe(18)  # ~24 chars
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _MASTER_FILE.write_text(code, encoding="utf-8")
    try:
        if os.name != "nt": os.chmod(_MASTER_FILE, 0o600)
    except Exception: pass
    print("\n" + "═" * 70 +
          "\n  HELEN-VAULT MASTER CODE (saved to "
          + str(_MASTER_FILE) + ")\n"
          "      " + code + "\n"
          "  Open: /vault/   (header: X-Vault-Token)\n"
          + "═" * 70 + "\n", flush=True)
    logger.warning("vault_master_code_generated", file=str(_MASTER_FILE))
    return code


def _read_master() -> Optional[str]:
    try:
        if _MASTER_FILE.is_file():
            return _MASTER_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return None


# ── Session store (in-memory only — deliberate) ──────────────────
_sessions: dict[str, dict] = {}


def _mint_session(ip: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "issued_at": time.time(),
        "expires_at": time.time() + SESSION_TTL,
        "remote_ip": ip,
        "actions": 0,
    }
    return token


def _verify_session(token: Optional[str]) -> Optional[dict]:
    if not token: return None
    s = _sessions.get(token)
    if not s: return None
    if s["expires_at"] < time.time():
        _sessions.pop(token, None)
        return None
    return s


# ── LAN enforcement ──────────────────────────────────────────────
_LAN_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),         # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
]


def _remote_ip(req: Request) -> str:
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        ip = xff.split(",")[0].strip()
        if ip: return ip
    if req.client and req.client.host:
        return req.client.host
    return "0.0.0.0"


def _is_lan(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _LAN_NETS)
    except ValueError:
        return False


def require_lan(req: Request) -> str:
    """Dependency that blocks non-LAN access. Returns the caller's IP."""
    ip = _remote_ip(req)
    if not _is_lan(ip):
        logger.warning("vault_public_access_denied", remote=ip)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "lan_only",
                    "reason": "vault is restricted to LAN addresses",
                    "your_ip": ip},
        )
    return ip


def require_session(req: Request, ip: str = Depends(require_lan)) -> dict:
    token = req.headers.get("X-Vault-Token")
    s = _verify_session(token)
    if not s:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="vault session invalid or expired")
    s["actions"] += 1
    return s


# ─────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────
class _AuthReq(BaseModel):
    code: str


@router.post("/auth")
async def vault_auth(body: _AuthReq, req: Request):
    _ = require_lan(req)
    master = _read_master()
    if not master:
        raise HTTPException(status_code=503, detail="vault master code uninitialized")
    provided = (body.code or "").strip()
    if not secrets.compare_digest(provided, master):
        logger.warning("vault_auth_fail", remote=_remote_ip(req))
        raise HTTPException(status_code=401, detail="invalid master code")
    token = _mint_session(_remote_ip(req))
    return {"token": token, "expires_in": SESSION_TTL,
            "note": "vault session — keep in memory only"}


@router.get("/session")
async def vault_session(s: dict = Depends(require_session)):
    return {
        "active":       True,
        "issued_at":    s["issued_at"],
        "expires_at":   s["expires_at"],
        "time_left_s":  int(s["expires_at"] - time.time()),
        "actions":      s["actions"],
        "remote_ip":    s["remote_ip"],
    }


# ─────────────────────────────────────────────────────────────────
# All secrets — unified view
# ─────────────────────────────────────────────────────────────────
@router.get("/all")
async def vault_all(s: dict = Depends(require_session)):
    """One-shot view: every secret in the system.

    Returns:
      {
        "admin_master": { fingerprint, created_at, sessions_active },
        "users": [ { id, username, display_name, role, avatar_url, share_code, ... } ],
        "access_codes": [ { code, owner, kind, note, ... } ],
      }
    """
    out = {"admin_master": {}, "users": [], "access_codes": []}

    # Admin master code: NEVER the plaintext — only a sha-256 short fingerprint.
    try:
        from app.api.routes.secret_admin import _read_master_code, _sessions as _secret_sessions
        mc = _read_master_code()
        if mc:
            fp = hashlib.sha256(mc.encode()).hexdigest()[:12]
            created = None
            try:
                sec_file = _DATA_DIR / "secret_master_code.txt"
                if sec_file.is_file():
                    created = sec_file.stat().st_mtime
            except Exception: pass
            out["admin_master"] = {
                "fingerprint":      fp,
                "length":           len(mc),
                "created_at":       created,
                "sessions_active":  len(_secret_sessions),
                "location":         str(_DATA_DIR / "secret_master_code.txt"),
            }
    except Exception as e:
        out["admin_master"] = {"error": str(e)}

    # Users with share_code + avatar
    try:
        from app.db.session import async_session_factory
        from sqlalchemy import text
        async with async_session_factory() as db:
            result = await db.execute(text(
                "SELECT id, username, display_name, role, avatar_url, "
                "share_code, status, created_at, last_seen "
                "FROM users ORDER BY created_at DESC LIMIT 500"
            ))
            for row in result:
                out["users"].append({
                    "id":            row[0],
                    "username":      row[1],
                    "display_name":  row[2],
                    "role":          row[3],
                    "avatar_url":    row[4],
                    "share_code":    row[5],
                    "status":        row[6],
                    "created_at":    str(row[7]) if row[7] else None,
                    "last_seen_at":  str(row[8]) if row[8] else None,
                })
    except Exception as e:
        out["users_error"] = str(e)

    # Access codes
    try:
        from app.services.access_codes_service import get_service
        out["access_codes"] = get_service().list_all()
    except Exception as e:
        out["access_codes_error"] = str(e)

    return out


# ─────────────────────────────────────────────────────────────────
# Code operations
# ─────────────────────────────────────────────────────────────────
class _CodeRevoke(BaseModel):
    code: str


@router.post("/codes/revoke")
async def vault_revoke(body: _CodeRevoke, s: dict = Depends(require_session)):
    from app.services.access_codes_service import get_service
    ok = get_service().admin_revoke(body.code, by_user_id=f"vault@{s['remote_ip']}")
    if not ok:
        raise HTTPException(status_code=404, detail="code not found")
    return {"revoked": True, "code": body.code}


class _CodeCreate(BaseModel):
    owner_user_id: str
    kind: str = "invite"
    note: str = ""
    max_uses: Optional[int] = None
    ttl_sec: Optional[int] = None


@router.post("/codes")
async def vault_create_code(body: _CodeCreate, s: dict = Depends(require_session)):
    """Admin-privilege mint: vault operator creates a code on behalf of a user."""
    from app.services.access_codes_service import get_service
    try:
        rec = get_service().create(
            owner_user_id=body.owner_user_id,
            kind=body.kind,
            note=f"[vault@{s['remote_ip']}] {body.note}"[:200],
            max_uses=body.max_uses,
            ttl_sec=body.ttl_sec,
        )
        return rec
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─────────────────────────────────────────────────────────────────
# Master code management — full control from inside the Vault.
#
# Reveal endpoints return PLAINTEXT. The Vault is the one place where
# this is acceptable: the operator already passed Vault auth, is bound
# to a LAN address, and the Vault's whole purpose is being the master
# key registry. Plaintext is gated behind ?reveal=true so a careless
# log accidentally hitting /admin-master doesn't leak the value.
# ─────────────────────────────────────────────────────────────────

class _SetMaster(BaseModel):
    new_code: str


def _validate_code(code: str) -> str:
    """Strip and length-check. Anything 4–256 chars is allowed (operator's call).
    The Vault itself is LAN+master-gated; weak codes are operator policy, not bug.
    """
    code = (code or "").strip()
    if len(code) < 4:
        raise HTTPException(status_code=400, detail="code must be at least 4 chars")
    if len(code) > 256:
        raise HTTPException(status_code=400, detail="code must be at most 256 chars")
    if any(ord(c) < 32 for c in code):
        raise HTTPException(status_code=400, detail="code must not contain control characters")
    return code


# ── Admin master code (the secret-admin realm) ─────────────────
@router.get("/admin-master")
async def vault_get_admin_master(reveal: bool = False,
                                  s: dict = Depends(require_session)):
    """Return admin master metadata. With ?reveal=true returns plaintext.

    Operator must explicitly pass reveal=true on the URL — we never
    return plaintext on the default path. The fingerprint is always safe.
    """
    try:
        from app.api.routes.secret_admin import _read_master_code, _MASTER_FILE as _SA_FILE
        mc = _read_master_code()
        if not mc:
            return {"present": False}
        out = {
            "present":     True,
            "fingerprint": hashlib.sha256(mc.encode()).hexdigest()[:12],
            "length":      len(mc),
            "file":        str(_SA_FILE),
        }
        if reveal:
            out["plaintext"] = mc
            logger.warning("vault_revealed_admin_master", by=s["remote_ip"])
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin-master")
async def vault_set_admin_master(body: _SetMaster,
                                  s: dict = Depends(require_session)):
    """Replace the admin master code with an operator-specified value.
    Invalidates every existing secret-admin session immediately.
    """
    code = _validate_code(body.new_code)
    try:
        from app.api.routes.secret_admin import _MASTER_FILE as _SA_FILE, _sessions as _SA_SESS
        _SA_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SA_FILE.write_text(code, encoding="utf-8")
        try:
            if os.name != "nt": os.chmod(_SA_FILE, 0o600)
        except Exception: pass
        _SA_SESS.clear()
        logger.warning("vault_set_admin_master", by=s["remote_ip"])
        return {"applied": True, "fingerprint":
                hashlib.sha256(code.encode()).hexdigest()[:12]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin-master/rotate")
async def vault_rotate_admin_master(s: dict = Depends(require_session)):
    """Generate a fresh random admin master code. Returns plaintext ONCE."""
    new = secrets.token_urlsafe(18)
    return await vault_set_admin_master(_SetMaster(new_code=new), s) | {
        "plaintext_once": new,
        "warning": "save this — not retrievable later without ?reveal=true on /admin-master"
    }


# ── Vault master code (this realm) ─────────────────────────────
@router.get("/vault-master")
async def vault_get_vault_master(reveal: bool = False,
                                  s: dict = Depends(require_session)):
    """Return vault master metadata. Plaintext only with ?reveal=true."""
    mc = _read_master()
    if not mc:
        return {"present": False}
    out = {
        "present":     True,
        "fingerprint": hashlib.sha256(mc.encode()).hexdigest()[:12],
        "length":      len(mc),
        "file":        str(_MASTER_FILE),
    }
    if reveal:
        out["plaintext"] = mc
        logger.warning("vault_revealed_vault_master", by=s["remote_ip"])
    return out


@router.post("/vault-master")
async def vault_set_vault_master(body: _SetMaster,
                                  s: dict = Depends(require_session)):
    """Replace the vault master with an operator-specified value.
    Invalidates the current vault session (you'll need to log back in).
    """
    code = _validate_code(body.new_code)
    _MASTER_FILE.write_text(code, encoding="utf-8")
    try:
        if os.name != "nt": os.chmod(_MASTER_FILE, 0o600)
    except Exception: pass
    _sessions.clear()
    logger.warning("vault_set_vault_master", by=s["remote_ip"])
    return {"applied": True,
            "fingerprint": hashlib.sha256(code.encode()).hexdigest()[:12]}


@router.post("/rotate-vault")
async def vault_rotate_self(s: dict = Depends(require_session)):
    """Rotate the vault master to a random value. Returns plaintext ONCE."""
    new = secrets.token_urlsafe(18)
    _MASTER_FILE.write_text(new, encoding="utf-8")
    try:
        if os.name != "nt": os.chmod(_MASTER_FILE, 0o600)
    except Exception: pass
    _sessions.clear()
    logger.warning("vault_master_rotated", by=s["remote_ip"])
    return {"rotated": True, "plaintext_once": new, "file": str(_MASTER_FILE)}


# ─────────────────────────────────────────────────────────────────
# Bulk access-code operations
# ─────────────────────────────────────────────────────────────────
class _BulkFilter(BaseModel):
    only_kind: Optional[str] = None
    only_owner_id: Optional[str] = None


@router.post("/codes/bulk-revoke-expired")
async def vault_bulk_revoke_expired(s: dict = Depends(require_session)):
    """Revoke every code whose expires_at has passed."""
    from app.services.access_codes_service import get_service
    svc = get_service()
    now = time.time()
    affected = []
    for c in svc.list_all():
        if c.get("revoked"): continue
        exp = c.get("expires_at")
        if exp and exp < now:
            svc.admin_revoke(c["code"], by_user_id=f"vault@{s['remote_ip']}")
            affected.append(c["code"])
    return {"revoked": len(affected), "codes": affected[:100]}


@router.post("/codes/purge-revoked")
async def vault_purge_revoked(s: dict = Depends(require_session)):
    """Permanently delete every revoked code from the store."""
    from app.services.access_codes_service import get_service
    svc = get_service()
    with svc._lock:
        before = len(svc._codes)
        svc._codes = {k: v for k, v in svc._codes.items() if not v.get("revoked")}
        after = len(svc._codes)
        svc._dirty = True
        svc._save()
    purged = before - after
    logger.warning("vault_purged_revoked", count=purged, by=s["remote_ip"])
    return {"purged": purged}


class _ExtendCode(BaseModel):
    code: str
    add_seconds: int = 86400
    add_uses: int = 0
    note: Optional[str] = None


@router.post("/codes/edit")
async def vault_edit_code(body: _ExtendCode, s: dict = Depends(require_session)):
    """Extend TTL, add uses, or update note for an existing code."""
    from app.services.access_codes_service import get_service
    svc = get_service()
    with svc._lock:
        rec = svc._codes.get(body.code.upper())
        if not rec:
            raise HTTPException(status_code=404, detail="code not found")
        if body.add_seconds:
            base = rec.get("expires_at") or time.time()
            rec["expires_at"] = base + max(0, int(body.add_seconds))
        if body.add_uses and rec.get("uses_remaining") is not None:
            rec["uses_remaining"] = max(0, rec["uses_remaining"] + int(body.add_uses))
            if rec.get("max_uses") is not None:
                rec["max_uses"] = rec["max_uses"] + int(body.add_uses)
        if body.note is not None:
            rec["note"] = (body.note or "")[:200]
        svc._dirty = True
        svc._save()
    return {"updated": True, "code": body.code}
