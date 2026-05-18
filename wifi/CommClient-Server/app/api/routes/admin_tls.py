"""
Admin — TLS certificate manager (Phase 2 / Module J).

Endpoints
---------
GET   /api/admin/tls/info             — current cert details
POST  /api/admin/tls/regenerate       — fresh self-signed pair
POST  /api/admin/tls/upload           — upload PEM cert + key (multipart)
POST  /api/admin/tls/acme/request     — request Let's Encrypt cert
GET   /api/admin/tls/acme/status      — current renewal/runner status

All write endpoints require ``system.config_write``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.core.audit import audit_log
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.rbac.enforcer import require_permission
from app.services.tls_manager import (
    acme_available,
    acme_request,
    inspect_cert,
    regenerate_self_signed,
    write_cert_pair,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/tls", tags=["admin-phase2"])


# ── Path resolution ───────────────────────────────────────

def _cert_paths() -> tuple[Path, Path]:
    """Where the running server reads its cert / key. Falls back to the
    conventional ``<data>/certs/helen.{crt,key}``."""
    s = get_settings()
    root = Path(s.PROJECT_ROOT)
    cert = Path(s.SSL_CERTFILE) if s.SSL_CERTFILE else root / "data" / "certs" / "helen.crt"
    key  = Path(s.SSL_KEYFILE)  if s.SSL_KEYFILE  else root / "data" / "certs" / "helen.key"
    return cert, key


# ── Models ────────────────────────────────────────────────

class RegenerateRequest(BaseModel):
    san_list: list[str] = Field(default_factory=list)
    days: int = Field(365, ge=1, le=3650)
    common_name: Optional[str] = None
    key_size: int = Field(2048, ge=2048, le=4096)


class AcmeRequest(BaseModel):
    domain: str
    email: str
    mode: Literal["http01", "dns01"] = "http01"
    staging: bool = False


# ── In-memory ACME runner status (single-process) ─────────

_ACME_STATE: dict = {
    "available": acme_available(),
    "last_run": None,
    "last_status": "idle",
    "last_message": "",
}


# ── Endpoints ─────────────────────────────────────────────

@router.get("/info")
async def info(
    user_id: str = Depends(require_permission("system.config_read")),
):
    cert_p, key_p = _cert_paths()
    if not cert_p.exists():
        return {
            "configured": False,
            "cert_path": str(cert_p),
            "key_path": str(key_p),
            "https_enabled": get_settings().HTTPS_ENABLED,
            "message": "no certificate on disk",
        }
    try:
        info = inspect_cert(cert_p)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cannot parse cert: {e}")
    return {
        "configured": True,
        "https_enabled": get_settings().HTTPS_ENABLED,
        "cert_path": str(cert_p),
        "key_path": str(key_p),
        "key_present": key_p.exists(),
        "cert": info.to_dict(),
    }


@router.post("/regenerate")
async def regenerate(
    body: RegenerateRequest,
    user_id: str = Depends(require_permission("system.config_write")),
):
    if not body.san_list:
        raise HTTPException(status_code=400, detail="san_list cannot be empty")
    try:
        cert_pem, key_pem = regenerate_self_signed(
            body.san_list, days=body.days,
            common_name=body.common_name, key_size=body.key_size,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"keygen failed: {e}")

    cert_p, key_p = _cert_paths()
    backups = write_cert_pair(cert_pem, key_pem, cert_p, key_p, backup=True)
    audit_log("admin.tls_regenerated", user_id=user_id, success=True,
              details={"san": body.san_list, "days": body.days,
                       "key_size": body.key_size})
    return {
        "ok": True,
        "cert": inspect_cert(cert_p).to_dict(),
        "backups": backups,
        "restart_required": True,
    }


@router.post("/upload")
async def upload(
    cert_file: UploadFile = File(...),
    key_file: UploadFile = File(...),
    note: str = Form(""),
    user_id: str = Depends(require_permission("system.config_write")),
):
    cert_pem = await cert_file.read()
    key_pem  = await key_file.read()
    if not cert_pem.lstrip().startswith(b"-----BEGIN"):
        raise HTTPException(status_code=400, detail="cert is not PEM")
    if not key_pem.lstrip().startswith(b"-----BEGIN"):
        raise HTTPException(status_code=400, detail="key is not PEM")

    # Round-trip parse to validate.
    cert_p, key_p = _cert_paths()
    tmp_cert = cert_p.with_suffix(cert_p.suffix + ".validate")
    tmp_cert.parent.mkdir(parents=True, exist_ok=True)
    tmp_cert.write_bytes(cert_pem)
    try:
        inspect_cert(tmp_cert)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid cert: {e}")
    finally:
        try: tmp_cert.unlink()
        except FileNotFoundError: pass

    backups = write_cert_pair(cert_pem, key_pem, cert_p, key_p, backup=True)
    audit_log("admin.tls_uploaded", user_id=user_id, success=True,
              details={"note": note})
    return {
        "ok": True,
        "cert": inspect_cert(cert_p).to_dict(),
        "backups": backups,
        "restart_required": True,
    }


@router.post("/acme/request")
async def acme_request_endpoint(
    body: AcmeRequest,
    user_id: str = Depends(require_permission("system.config_write")),
):
    if not acme_available():
        raise HTTPException(
            status_code=501,
            detail="ACME library not installed (pip install acme josepy)",
        )
    _ACME_STATE["last_run"] = time.time()
    _ACME_STATE["last_status"] = "running"
    _ACME_STATE["last_message"] = f"requesting {body.domain}"

    result = acme_request(body.domain, body.email,
                          mode=body.mode, staging=body.staging)
    _ACME_STATE["last_status"] = "ok" if result.success else "error"
    _ACME_STATE["last_message"] = result.message

    if not result.success:
        audit_log("admin.tls_acme_request", user_id=user_id, success=False,
                  details={"domain": body.domain, "mode": body.mode,
                           "error": result.message})
        raise HTTPException(status_code=502, detail=result.message)

    cert_p, key_p = _cert_paths()
    backups = write_cert_pair(result.cert_pem, result.key_pem,
                              cert_p, key_p, backup=True)
    audit_log("admin.tls_acme_request", user_id=user_id, success=True,
              details={"domain": body.domain, "mode": body.mode,
                       "staging": body.staging})
    return {
        "ok": True,
        "cert": inspect_cert(cert_p).to_dict(),
        "backups": backups,
        "restart_required": True,
    }


@router.get("/acme/status")
async def acme_status(
    user_id: str = Depends(require_permission("system.config_read")),
):
    return dict(_ACME_STATE)
