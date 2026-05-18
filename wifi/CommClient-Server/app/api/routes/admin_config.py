"""
Admin — Live configuration editor (Phase 2 / Module I).

Endpoints
---------
GET  /api/admin/config/schema    — introspected pydantic Settings schema
GET  /api/admin/config/current   — current values (secrets masked)
POST /api/admin/config/preview   — validate proposed changes (no write)
POST /api/admin/config/apply     — apply changes (live or via .env)
GET  /api/admin/config/history   — past config changes from audit chain

Permission
----------
All write endpoints require ``system.config_write``.
Reads require ``system.config_read``.

Hot-reload integration
----------------------
If ``app.core.config_hot_reload`` exists and exposes ``apply(updates: dict)``,
we call it and report ``restart_required: False``. Otherwise we persist the
changes into the project's ``.env`` file at ``PROJECT_ROOT/.env`` and report
``restart_required: True``. Either path writes a ``system.config_changed``
event to the audit chain when one is configured.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.audit_chain import get_audit_chain
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/config", tags=["admin-phase2"])


# ── Secret-mask heuristics ────────────────────────────────

_SECRET_TOKENS = (
    "secret", "password", "passwd", "token", "key",
    "credential", "private", "salt", "auth",
)


def _is_secret(name: str) -> bool:
    n = name.lower()
    return any(tok in n for tok in _SECRET_TOKENS)


def _mask(v: Any) -> str:
    if v is None or v == "":
        return ""
    return "**" + (str(v)[-4:] if len(str(v)) >= 4 else "**")


def _categorize(field_name: str) -> str:
    n = field_name.upper()
    if n.startswith(("JWT_", "AUTH", "PASSWORD")):
        return "Auth"
    if n.startswith(("HTTPS_", "SSL_")):
        return "TLS"
    if n.startswith(("HOST", "PORT", "DEBUG", "LOG_", "PROJECT_")):
        return "Server"
    if n.startswith(("DB_", "DATABASE_", "SQLITE_")):
        return "Database"
    if n.startswith(("STUN_", "TURN_", "ICE_", "MEDIASOUP_")):
        return "Media / WebRTC"
    if n.startswith(("FEDERATION", "PEER", "DISCOVERY")):
        return "Federation"
    if n.startswith(("UPLOAD_", "ALLOWED_", "MAX_UPLOAD")):
        return "Uploads"
    if n.startswith(("RATE_", "MAX_SESSIONS")):
        return "Rate-limiting"
    if n.startswith(("AUTO_BACKUP", "BACKUP_")):
        return "Backups"
    if n.startswith(("TCP_FALLBACK",)):
        return "Networking"
    return "Other"


# ── Schema introspection ──────────────────────────────────

def _schema() -> list[dict[str, Any]]:
    """Return one entry per Settings field with type, default, current, secret flag."""
    from pydantic_core import PydanticUndefined                     # type: ignore

    settings = get_settings()
    fields = Settings.model_fields
    out: list[dict[str, Any]] = []
    for name, info in fields.items():
        current = getattr(settings, name, None)
        annotation = info.annotation
        type_name = getattr(annotation, "__name__", str(annotation))
        default = info.default
        if default is PydanticUndefined:
            default = None
        out.append({
            "name": name,
            "type": type_name,
            "default": _serialise(default),
            "current": _mask(current) if _is_secret(name) else _serialise(current),
            "secret": _is_secret(name),
            "description": (info.description or "").strip(),
            "category": _categorize(name),
        })
    out.sort(key=lambda d: (d["category"], d["name"]))
    return out


@router.get("/schema")
async def schema(
    user_id: str = Depends(require_permission("system.config_read")),
):
    return {"fields": _schema(), "categories": sorted({
        f["category"] for f in _schema()
    })}


@router.get("/current")
async def current(
    user_id: str = Depends(require_permission("system.config_read")),
):
    settings = get_settings()
    out: dict[str, Any] = {}
    for name in Settings.model_fields:
        val = getattr(settings, name, None)
        out[name] = _mask(val) if _is_secret(name) else _serialise(val)
    return out


def _serialise(v: Any) -> Any:
    if isinstance(v, Path):
        return str(v)
    return v


# ── Preview ───────────────────────────────────────────────

class ConfigChange(BaseModel):
    updates: dict[str, Any]


@router.post("/preview")
async def preview(
    body: ConfigChange,
    user_id: str = Depends(require_permission("system.config_read")),
):
    """Validate the proposed updates against the Settings model without
    touching the running instance."""
    if not body.updates:
        raise HTTPException(status_code=400, detail="no updates")
    current = get_settings().model_dump()
    merged = {**current, **body.updates}
    try:
        proposed = Settings.model_validate(merged)
    except ValidationError as e:
        return {"ok": False, "errors": json.loads(e.json())}
    diff = {}
    for k, v in body.updates.items():
        before = current.get(k)
        after = getattr(proposed, k, None)
        diff[k] = {
            "before": _mask(before) if _is_secret(k) else _serialise(before),
            "after":  _mask(after)  if _is_secret(k) else _serialise(after),
        }
    return {"ok": True, "diff": diff}


# ── Apply ─────────────────────────────────────────────────

def _try_hot_reload(updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    """If a hot-reload service exists, call it and return its result."""
    try:
        mod = importlib.import_module("app.core.config_hot_reload")
    except ImportError:
        return None
    fn = getattr(mod, "apply", None)
    if not callable(fn):
        return None
    return fn(updates)


def _persist_to_env_file(updates: dict[str, Any]) -> Path:
    settings = get_settings()
    env_path = Path(settings.PROJECT_ROOT) / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    keys_to_set = {k.upper(): v for k, v in updates.items()}
    seen: set[str] = set()
    out_lines: list[str] = []
    for ln in lines:
        if "=" in ln and not ln.lstrip().startswith("#"):
            k = ln.split("=", 1)[0].strip().upper()
            if k in keys_to_set:
                out_lines.append(f"{k}={_env_serialise(keys_to_set[k])}")
                seen.add(k); continue
        out_lines.append(ln)
    for k, v in keys_to_set.items():
        if k not in seen:
            out_lines.append(f"{k}={_env_serialise(v)}")

    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return env_path


def _env_serialise(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return str(v)


@router.post("/apply")
async def apply(
    body: ConfigChange,
    user_id: str = Depends(require_permission("system.config_write")),
):
    if not body.updates:
        raise HTTPException(status_code=400, detail="no updates")

    # 1) Validate first
    current = get_settings().model_dump()
    merged = {**current, **body.updates}
    try:
        Settings.model_validate(merged)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=json.loads(e.json()))

    # 2) Try hot-reload, else persist .env
    result = _try_hot_reload(body.updates)
    if result is not None:
        out = {"ok": True, "restart_required": False, "applied": body.updates,
               "details": result}
    else:
        env_path = _persist_to_env_file(body.updates)
        # Also update in-process env so subsequent get_settings() picks up
        # rate-limited keys on next cache flush (the lru_cache is cleared).
        for k, v in body.updates.items():
            os.environ[k.upper()] = _env_serialise(v)
        try:
            get_settings.cache_clear()                           # type: ignore[attr-defined]
        except Exception:
            pass
        out = {"ok": True, "restart_required": True,
               "applied": body.updates, "env_path": str(env_path)}

    # 3) Audit
    chain = get_audit_chain()
    if chain is not None:
        try:
            masked = {
                k: (_mask(v) if _is_secret(k) else v)
                for k, v in body.updates.items()
            }
            chain.append("system.config_changed", "system.config_changed",
                         target=None,
                         payload={"actor": user_id, "updates": masked,
                                  "restart_required": out["restart_required"]})
        except Exception:
            pass
    return out


# ── History ───────────────────────────────────────────────

@router.get("/history")
async def history(
    user_id: str = Depends(require_permission("system.config_read")),
    limit: int = 100,
):
    chain = get_audit_chain()
    if chain is None:
        return {"changes": [], "note": "audit chain not configured"}
    items: list[dict[str, Any]] = []
    for entry in chain.filter(action="system.config_changed", limit=limit):
        items.append({
            "seq": entry.seq,
            "timestamp": entry.timestamp,
            "actor": entry.actor,
            "payload": entry.payload,
            "chain_hash": entry.chain_hash,
        })
    return {"changes": items, "count": len(items)}
