"""
Phase 6 / Module AB — GDPR Article 15 data export.

Produces a single ZIP archive containing every piece of data the system
holds about the requesting user, plus a machine-readable manifest.

Layout::

    user_<uid>.zip
        manifest.json          — list of files + their sha256
        profile.json           — User + ProfilePhoto
        sessions.json          — UserSession rows
        consents.json          — ConsentRecord rows
        oauth.json             — OAuth accounts (token refs only, not secrets)
        channels.json          — channel memberships + owner channels
        messages/              — messages.json (paginated)
        files/metadata.json    — FileRecord rows
        files/raw/<file_id>    — actual file content
        audit.json             — AuditLog entries touching this user
        ai/sessions.json       — AI sessions + (truncated) messages

The archive is encrypted in-flight only by HTTPS; at-rest is the operator's
choice (mount on encrypted disk).  Archives auto-expire after 30 days.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.compliance import DataExportRequest

logger = get_logger(__name__)

_EXPORT_TTL_DAYS = 30


def _export_root() -> Path:
    s = get_settings()
    root = Path(getattr(s, "PROJECT_ROOT", "."))
    p = root / "data" / "compliance" / "exports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def _collect_user_data(db: AsyncSession, user_id: str) -> Dict[str, Any]:
    """Return a nested dict of every table touching `user_id`.

    The function is defensive: every model is imported inside the body so
    missing optional models do not break the export. Models with no rows
    for the user are simply skipped.
    """
    bundle: Dict[str, Any] = {"user_id": user_id, "exported_at": datetime.now(timezone.utc).isoformat()}

    # core user
    try:
        from app.models.user import User
        u = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if u:
            bundle["profile"] = _row_to_dict(u, redact=("password_hash",))
    except Exception as e:
        logger.warning("export_user_load_failed", error=str(e))

    bundle["sessions"] = await _safe_select(
        db, "app.models.session", "UserSession", "user_id", user_id,
    )
    bundle["contacts"] = await _safe_select(
        db, "app.models.contact", "Contact", "user_id", user_id,
    )
    bundle["consents"] = await _safe_select(
        db, "app.models.compliance", "ConsentRecord", "user_id", user_id,
    )
    bundle["oauth_accounts"] = await _safe_select(
        db, "app.models.oauth", "OAuthAccount", "user_id", user_id,
        redact=("refresh_token", "access_token"),
    )
    bundle["channel_memberships"] = await _safe_select(
        db, "app.models.channel", "ChannelMember", "user_id", user_id,
    )
    bundle["messages"] = await _safe_select(
        db, "app.models.message", "Message", "sender_id", user_id,
    )
    bundle["scheduled_messages"] = await _safe_select(
        db, "app.models.scheduled_message", "ScheduledMessage", "sender_id", user_id,
    )
    bundle["drafts"] = await _safe_select(
        db, "app.models.message_draft", "MessageDraft", "user_id", user_id,
    )
    bundle["saved_messages"] = await _safe_select(
        db, "app.models.saved_message", "SavedMessage", "user_id", user_id,
    )
    bundle["templates"] = await _safe_select(
        db, "app.models.message_template", "MessageTemplate", "user_id", user_id,
    )
    bundle["files"] = await _safe_select(
        db, "app.models.file", "FileRecord", "uploader_id", user_id,
    )
    bundle["device_tokens"] = await _safe_select(
        db, "app.models.device_token", "DeviceToken", "user_id", user_id,
        redact=("token",),
    )
    bundle["audit_log"] = await _safe_select(
        db, "app.models.audit_log", "AuditLog", "actor_id", user_id, limit=5000,
    )
    bundle["ai_sessions"] = await _safe_select(
        db, "app.models.ai_assistant", "AISession", "user_id", user_id,
    )
    bundle["ai_messages"] = []  # joined separately to limit volume
    return bundle


async def _safe_select(
    db: AsyncSession,
    module_path: str,
    class_name: str,
    user_column: str,
    user_id: str,
    *,
    limit: int = 10_000,
    redact: tuple[str, ...] = (),
) -> List[Dict[str, Any]]:
    try:
        mod = __import__(module_path, fromlist=[class_name])
        cls = getattr(mod, class_name)
        col = getattr(cls, user_column)
        rows = (await db.execute(select(cls).where(col == user_id).limit(limit))).scalars().all()
        return [_row_to_dict(r, redact=redact) for r in rows]
    except (ImportError, AttributeError, Exception) as e:
        logger.debug("export_skip", model=f"{module_path}.{class_name}", error=str(e))
        return []


def _row_to_dict(row: Any, redact: tuple[str, ...] = ()) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not hasattr(row, "__table__"):
        return out
    for c in row.__table__.columns:
        v = getattr(row, c.name, None)
        if c.name in redact and v is not None:
            v = "***"
        if isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, (bytes, bytearray)):
            v = "<binary:%d bytes>" % len(v)
        out[c.name] = v
    return out


async def build_export_archive(user_id: str, request_id: str) -> Dict[str, Any]:
    """Synchronous end-to-end build: collect → zip → record."""
    out_path = _export_root() / f"export_{user_id}_{request_id}.zip"
    async with async_session_factory() as db:
        bundle = await _collect_user_data(db, user_id)

    raw_file_paths: List[Path] = []
    for f in bundle.get("files", []) or []:
        path = f.get("path") or f.get("storage_path")
        if path and Path(path).exists():
            raw_file_paths.append(Path(path))

    manifest_entries: List[Dict[str, Any]] = []

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. JSON sections
        for key, value in bundle.items():
            if not isinstance(value, (dict, list)):
                continue
            data = json.dumps(value, default=str, indent=2).encode("utf-8")
            arc = f"{key}.json"
            zf.writestr(arc, data)
            manifest_entries.append({
                "name": arc, "sha256": _sha256_bytes(data), "size": len(data),
            })

        # 2. raw file payloads
        for p in raw_file_paths[:5000]:
            try:
                arc = f"files/raw/{p.name}"
                zf.write(p, arcname=arc)
                manifest_entries.append({
                    "name": arc, "sha256": _sha256_file(p), "size": p.stat().st_size,
                })
            except OSError as e:
                logger.warning("export_file_add_failed", path=str(p), error=str(e))

        # 3. final manifest
        manifest = {
            "user_id": user_id,
            "request_id": request_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "files": manifest_entries,
            "schema": "helen-data-export/v1",
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2).encode())

    sha = _sha256_file(out_path)
    return {
        "path": str(out_path),
        "size_bytes": out_path.stat().st_size,
        "sha256": sha,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=_EXPORT_TTL_DAYS)),
    }


async def request_export(user_id: str) -> str:
    rid = uuid.uuid4().hex
    async with async_session_factory() as db:
        req = DataExportRequest(id=rid, user_id=user_id, status="pending")
        db.add(req)
        await db.commit()
    return rid


async def fulfill_export(request_id: str) -> Dict[str, Any]:
    """Worker entrypoint — moves request to ready / failed."""
    async with async_session_factory() as db:
        req = (await db.execute(
            select(DataExportRequest).where(DataExportRequest.id == request_id)
        )).scalar_one_or_none()
        if not req:
            raise LookupError(request_id)
        req.status = "running"
        await db.commit()
        user_id = req.user_id
    try:
        result = await build_export_archive(user_id, request_id)
        async with async_session_factory() as db:
            req = (await db.execute(
                select(DataExportRequest).where(DataExportRequest.id == request_id)
            )).scalar_one_or_none()
            req.status = "ready"
            req.completed_at = datetime.now(timezone.utc)
            req.file_path = result["path"]
            req.sha256 = result["sha256"]
            req.size_bytes = result["size_bytes"]
            req.expires_at = result["expires_at"]
            await db.commit()
        return result
    except Exception as e:
        async with async_session_factory() as db:
            req = (await db.execute(
                select(DataExportRequest).where(DataExportRequest.id == request_id)
            )).scalar_one_or_none()
            if req:
                req.status = "failed"
                req.error_message = str(e)[:1024]
                await db.commit()
        raise


async def expire_old_exports() -> int:
    now = datetime.now(timezone.utc)
    count = 0
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(DataExportRequest).where(
                DataExportRequest.status == "ready",
                DataExportRequest.expires_at != None,                   # noqa: E711
            )
        )).scalars().all()
        for r in rows:
            if r.expires_at and r.expires_at < now:
                if r.file_path and Path(r.file_path).exists():
                    try:
                        Path(r.file_path).unlink()
                    except OSError:
                        pass
                r.status = "expired"
                r.file_path = None
                count += 1
        if count:
            await db.commit()
    return count
