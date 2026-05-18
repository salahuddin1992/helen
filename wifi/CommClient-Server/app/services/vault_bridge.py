"""
Phase 3 / Module P — Vault subsystem bridge.

The admin file UI can show both *unencrypted* (FileRecord) and *encrypted*
(Vault) assets in one view. This module mediates between the FastAPI
service layer and the optional ``wifi/Vault/`` subsystem.

If the Vault subsystem is absent (or its HTTP API is down), every call
degrades to ``None`` / empty list / "vault_unavailable" status. The admin
UI hides Vault-specific controls in that case.

Vault is expected to expose (via local IPC or HTTP on 127.0.0.1) the
following minimal API:

  GET  /vault/list?workspace_id=…       -> [{id, name, size, mime, …}]
  GET  /vault/decrypt/{id}              -> binary stream
  GET  /vault/status                    -> {"ok": bool, "version": "…"}

We do not couple to its internals — anything else is best-effort.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.core.audit import audit_log
from app.core.logging import get_logger

logger = get_logger(__name__)

_VAULT_BASE_URL = os.environ.get(
    "HELEN_VAULT_BASE_URL", "http://127.0.0.1:7777"
).rstrip("/")
_VAULT_TIMEOUT = float(os.environ.get("HELEN_VAULT_TIMEOUT", "2.0"))
_VAULT_TOKEN = os.environ.get("HELEN_VAULT_TOKEN", "")


@dataclass
class VaultStatus:
    available: bool
    version: Optional[str] = None
    detail: Optional[str] = None


async def _vault_request(
    method: str, path: str, **kw: Any,
) -> Optional[httpx.Response]:
    headers = kw.pop("headers", {}) or {}
    if _VAULT_TOKEN:
        headers["Authorization"] = f"Bearer {_VAULT_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=_VAULT_TIMEOUT) as client:
            r = await client.request(
                method, f"{_VAULT_BASE_URL}{path}", headers=headers, **kw,
            )
            return r
    except Exception as exc:                                       # pragma: no cover
        logger.warning("vault_bridge_offline", error=str(exc))
        return None


async def get_status() -> VaultStatus:
    r = await _vault_request("GET", "/vault/status")
    if r is None:
        return VaultStatus(available=False, detail="vault subsystem unreachable")
    if r.status_code != 200:
        return VaultStatus(available=False, detail=f"HTTP {r.status_code}")
    try:
        body = r.json()
    except Exception:
        return VaultStatus(available=False, detail="invalid status body")
    return VaultStatus(
        available=bool(body.get("ok", False)),
        version=body.get("version"),
        detail=body.get("detail"),
    )


async def list_encrypted_files(
    workspace_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    params = {}
    if workspace_id:
        params["workspace_id"] = workspace_id
    r = await _vault_request("GET", "/vault/list", params=params)
    if r is None or r.status_code != 200:
        return []
    try:
        body = r.json()
    except Exception:
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        return body["items"]
    return []


async def decrypt_for_preview(
    file_id: str, requester_user_id: str,
) -> Optional[bytes]:
    """Audit-logged decrypt — every preview is recorded."""
    audit_log(
        "vault.decrypt_requested",
        user_id=requester_user_id, success=True,
        details={"vault_id": file_id},
    )
    r = await _vault_request("GET", f"/vault/decrypt/{file_id}")
    if r is None:
        audit_log("vault.decrypt_unreachable", user_id=requester_user_id,
                  success=False, details={"vault_id": file_id})
        return None
    if r.status_code != 200:
        audit_log("vault.decrypt_failed", user_id=requester_user_id,
                  success=False,
                  details={"vault_id": file_id, "status": r.status_code})
        return None
    return r.content


async def delete_encrypted_file(
    file_id: str, requester_user_id: str,
) -> bool:
    r = await _vault_request("DELETE", f"/vault/{file_id}")
    success = bool(r and r.status_code in (200, 204))
    audit_log(
        "vault.delete",
        user_id=requester_user_id, success=success,
        details={"vault_id": file_id},
    )
    return success
