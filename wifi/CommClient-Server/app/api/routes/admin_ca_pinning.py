"""
FastAPI router for External CA Pinning administration.

Endpoints (all under `/api/admin/security/pinning`):

  GET    /pins                        list all pins
  POST   /pins                        add a pin
  DELETE /pins/{host}/{pin_value}     remove a pin
  POST   /pins/learn                  TOFU: learn pin from PEM
  POST   /pins/rotate                 rotate pin with grace window
  POST   /pins/prune                  prune expired pins
  POST   /pins/validate               validate a chain against pins

  GET    /ca-bundles                  list CA bundles
  POST   /ca-bundles                  add CA bundle (PEM)
  DELETE /ca-bundles/{name}           remove CA bundle

  GET    /export                      export pins+bundles as JSON
  POST   /import                      import pins+bundles from JSON
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, Field

try:
    from app.api.deps import require_admin  # type: ignore
except Exception:  # pragma: no cover
    try:
        from app.core.security_utils import require_role  # type: ignore

        require_admin = require_role("admin")  # type: ignore
    except Exception:
        async def require_admin():  # type: ignore
            return {"sub": "anonymous"}

from app.services.security.ca_pinning import (
    CAPinningService,
    CertificatePin,
    PinSource,
    PinType,
    PinValidationError,
    get_ca_pinning_service,
)

log = logging.getLogger("helen.api.admin_ca_pinning")

router = APIRouter(prefix="/api/admin/security/pinning", tags=["admin-ca-pinning"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AddPinRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    pin_type: str = Field(..., description="cert-sha256 | spki-sha256 | ca-cert-sha256")
    value: str = Field(..., min_length=1, description="base64 or hex hash")
    description: str = ""
    source: str = "operator"
    expires_at: Optional[str] = None
    rotation_group: Optional[str] = None


class LearnPinRequest(BaseModel):
    host: str
    chain_pem: str = Field(..., description="PEM-encoded cert chain")


class RotatePinRequest(BaseModel):
    host: str
    old_pin_value: str
    new_pin_value: str
    pin_type: str = "spki-sha256"
    grace_days: int = Field(7, ge=1, le=365)


class ValidateChainRequest(BaseModel):
    host: str
    chain_pem: str
    check_expiry: bool = True
    require_pin: bool = True


class AddCABundleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    pem: str = Field(..., description="PEM-encoded CA chain (one or more certs)")
    description: str = ""


class ImportPinsRequest(BaseModel):
    data: Dict[str, Any]
    merge: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _svc() -> CAPinningService:
    return get_ca_pinning_service()


def _serialize_pin(p: CertificatePin) -> Dict[str, Any]:
    return p.to_dict()


def _parse_pin_type(s: str) -> PinType:
    try:
        return PinType(s)
    except ValueError as exc:
        raise HTTPException(400, f"invalid pin_type: {s}") from exc


def _parse_source(s: str) -> PinSource:
    try:
        return PinSource(s)
    except ValueError as exc:
        raise HTTPException(400, f"invalid source: {s}") from exc


def _actor_id(claims: Any) -> Optional[str]:
    if isinstance(claims, dict):
        return claims.get("sub") or claims.get("username") or claims.get("user_id")
    return None


def _audit(action: str, payload: Dict[str, Any], actor: Optional[str] = None) -> None:
    try:
        from app.services.audit_chain import get_audit_chain  # type: ignore

        get_audit_chain().append(actor=actor or "admin", action=action, target=None, payload=payload)
    except Exception:
        try:
            from app.core.audit import audit_log  # type: ignore

            audit_log(action, user_id=actor, success=True, details=payload)
        except Exception:
            log.info("audit:%s actor=%s payload=%s", action, actor, payload)


# ---------------------------------------------------------------------------
# Pin endpoints
# ---------------------------------------------------------------------------


@router.get("/pins")
async def list_pins(
    host: Optional[str] = None,
    claims: Any = Depends(require_admin),
) -> Dict[str, Any]:
    pins = _svc().list_pins(host=host)
    return {
        "count": len(pins),
        "host_count": len({p.host for p in pins}),
        "pins": [_serialize_pin(p) for p in pins],
    }


@router.post("/pins")
async def add_pin(
    body: AddPinRequest,
    claims: Any = Depends(require_admin),
) -> Dict[str, Any]:
    svc = _svc()
    actor = _actor_id(claims)
    pin = svc.add_pin(
        host=body.host,
        pin_type=_parse_pin_type(body.pin_type),
        value=body.value,
        source=_parse_source(body.source),
        description=body.description,
        added_by=actor,
        expires_at=body.expires_at,
        rotation_group=body.rotation_group,
    )
    _audit("security.pin.add", _serialize_pin(pin), actor)
    return {"ok": True, "pin": _serialize_pin(pin)}


@router.delete("/pins/{host}/{pin_value}")
async def remove_pin(
    host: str = Path(..., min_length=1),
    pin_value: str = Path(..., min_length=4),
    pin_type: str = "spki-sha256",
    claims: Any = Depends(require_admin),
) -> Dict[str, Any]:
    svc = _svc()
    actor = _actor_id(claims)
    removed = svc.remove_pin(host=host, pin_type=_parse_pin_type(pin_type), value=pin_value, removed_by=actor)
    if not removed:
        raise HTTPException(404, "pin not found")
    _audit("security.pin.remove", {"host": host, "pin_type": pin_type, "value_prefix": pin_value[:12]}, actor)
    return {"ok": True}


@router.post("/pins/learn")
async def learn_pin(
    body: LearnPinRequest,
    claims: Any = Depends(require_admin),
) -> Dict[str, Any]:
    svc = _svc()
    actor = _actor_id(claims)
    try:
        pin = svc.learn_pin(host=body.host, chain_pem=body.chain_pem, added_by=actor)
    except Exception as exc:
        raise HTTPException(400, f"learn failed: {exc}") from exc
    _audit("security.pin.learn", _serialize_pin(pin), actor)
    return {"ok": True, "pin": _serialize_pin(pin), "note": "TOFU pin learned"}


@router.post("/pins/rotate")
async def rotate_pin(
    body: RotatePinRequest,
    claims: Any = Depends(require_admin),
) -> Dict[str, Any]:
    svc = _svc()
    actor = _actor_id(claims)
    try:
        old_obj, new_obj = svc.rotate_pin(
            host=body.host,
            old_pin_value=body.old_pin_value,
            new_pin_value=body.new_pin_value,
            pin_type=_parse_pin_type(body.pin_type),
            grace_seconds=body.grace_days * 86400,
            added_by=actor,
        )
    except Exception as exc:
        raise HTTPException(400, f"rotate failed: {exc}") from exc
    _audit(
        "security.pin.rotate",
        {"host": body.host, "old_value_prefix": body.old_pin_value[:12], "grace_days": body.grace_days},
        actor,
    )
    return {"ok": True, "old": _serialize_pin(old_obj), "new": _serialize_pin(new_obj)}


@router.post("/pins/prune")
async def prune_pins(claims: Any = Depends(require_admin)) -> Dict[str, Any]:
    svc = _svc()
    removed = svc.prune_expired_pins()
    _audit("security.pin.prune", {"removed": removed}, _actor_id(claims))
    return {"ok": True, "removed": removed}


@router.post("/pins/validate")
async def validate_chain(
    body: ValidateChainRequest,
    claims: Any = Depends(require_admin),
) -> Dict[str, Any]:
    svc = _svc()
    result = svc.validate_chain(
        host=body.host,
        chain_pem=body.chain_pem,
        check_expiry=body.check_expiry,
        require_pin=body.require_pin,
    )
    return {
        "valid": result.valid,
        "host": result.host,
        "chain_depth": result.chain_depth,
        "leaf": {
            "subject": result.leaf_subject,
            "issuer": result.leaf_issuer,
            "not_before": result.leaf_not_before,
            "not_after": result.leaf_not_after,
            "san": result.leaf_san,
            "sha256": result.leaf_sha256,
            "spki_sha256": result.leaf_spki_sha256,
        },
        "matched_pin": _serialize_pin(result.matched_pin) if result.matched_pin else None,
        "errors": result.errors,
        "warnings": result.warnings,
    }


# ---------------------------------------------------------------------------
# CA bundle endpoints
# ---------------------------------------------------------------------------


@router.get("/ca-bundles")
async def list_ca_bundles(claims: Any = Depends(require_admin)) -> Dict[str, Any]:
    svc = _svc()
    bundles = svc.list_ca_bundles()
    return {
        "count": len(bundles),
        "bundles": [
            {
                "name": b.name,
                "description": b.description,
                "added_at": b.added_at,
                "added_by": b.added_by,
                "enabled": b.enabled,
                "pem_size_bytes": len(b.pem),
            }
            for b in bundles
        ],
    }


@router.post("/ca-bundles")
async def add_ca_bundle(
    body: AddCABundleRequest,
    claims: Any = Depends(require_admin),
) -> Dict[str, Any]:
    svc = _svc()
    actor = _actor_id(claims)
    try:
        bundle = svc.add_ca_bundle(
            name=body.name, pem=body.pem, description=body.description, added_by=actor
        )
    except Exception as exc:
        raise HTTPException(400, f"add bundle failed: {exc}") from exc
    _audit("security.ca_bundle.add", {"name": bundle.name, "size": len(bundle.pem)}, actor)
    return {"ok": True, "bundle": {"name": bundle.name, "added_at": bundle.added_at}}


@router.delete("/ca-bundles/{name}")
async def remove_ca_bundle(name: str, claims: Any = Depends(require_admin)) -> Dict[str, Any]:
    svc = _svc()
    if not svc.remove_ca_bundle(name):
        raise HTTPException(404, "bundle not found")
    _audit("security.ca_bundle.remove", {"name": name}, _actor_id(claims))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------


@router.get("/export")
async def export_pins(claims: Any = Depends(require_admin)) -> Dict[str, Any]:
    return _svc().export_json()


@router.post("/import")
async def import_pins(
    body: ImportPinsRequest,
    claims: Any = Depends(require_admin),
) -> Dict[str, Any]:
    svc = _svc()
    actor = _actor_id(claims)
    added = svc.import_json(body.data, merge=body.merge, imported_by=actor)
    _audit("security.pin.import", {"added": added, "merge": body.merge}, actor)
    return {"ok": True, "added": added}
