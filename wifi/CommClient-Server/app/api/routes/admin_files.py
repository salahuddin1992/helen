"""
Phase 3 / Module P — Admin file vault endpoints.

Routes
------
GET    /api/admin/files/browse            list files in UPLOAD_DIR (path-safe)
GET    /api/admin/files/preview/{id}      preview image/text inline
GET    /api/admin/files/download/{id}     attachment download
DELETE /api/admin/files/{id}              delete (audit-logged)
GET    /api/admin/files/stats             total count / size / by type
POST   /api/admin/files/cleanup           bulk delete by filter
GET    /api/admin/files/upload-monitor    in-progress chunked uploads
GET    /api/admin/files/vault             encrypted-files bridge view
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.config import get_settings
from app.core.deps import get_db
from app.core.logging import get_logger
from app.core.security_utils import require_role
from app.models.file import FileRecord
from app.services import vault_bridge

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin/files", tags=["admin-files"])
settings = get_settings()


# ── helpers ────────────────────────────────────────────────

def _upload_root() -> Path:
    return settings.upload_path.resolve()


def _safe_join(rel: str) -> Path:
    """Path-traversal-safe join under UPLOAD_DIR."""
    root = _upload_root()
    candidate = (root / rel).resolve()
    if not str(candidate).startswith(str(root)):
        raise HTTPException(status_code=400, detail="Invalid path.")
    return candidate


def _classify(mime: str) -> str:
    mime = (mime or "").lower()
    if mime.startswith("image/"): return "image"
    if mime.startswith("video/"): return "video"
    if mime.startswith("audio/"): return "audio"
    if mime in ("application/pdf",): return "pdf"
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return "text"
    if mime.startswith("application/"): return "binary"
    return "other"


# ── Browse ─────────────────────────────────────────────────

class BrowseItem(BaseModel):
    name: str
    rel_path: str
    is_dir: bool
    size: int
    mtime: float
    mime: Optional[str] = None


@router.get("/browse", response_model=list[BrowseItem])
async def browse(
    path: str = Query("", description="relative path inside UPLOAD_DIR"),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=2000),
    user_id: str = Depends(require_role("admin")),
) -> list[BrowseItem]:
    target = _safe_join(path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found.")
    items: list[BrowseItem] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            st = child.stat()
        except OSError:
            continue
        items.append(BrowseItem(
            name=child.name,
            rel_path=str(child.relative_to(_upload_root())),
            is_dir=child.is_dir(),
            size=st.st_size,
            mtime=st.st_mtime,
        ))
    start = (page - 1) * page_size
    return items[start:start + page_size]


# ── Stats ──────────────────────────────────────────────────

class StatsOut(BaseModel):
    total_files: int
    total_bytes: int
    by_type: dict[str, int]
    by_type_bytes: dict[str, int]
    largest: list[dict[str, Any]]
    oldest: list[dict[str, Any]]


@router.get("/stats", response_model=StatsOut)
async def stats(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_role("admin")),
) -> StatsOut:
    total_files = await db.scalar(select(func.count()).select_from(FileRecord)) or 0
    total_bytes = await db.scalar(select(func.coalesce(func.sum(FileRecord.size_bytes), 0))) or 0

    rows = await db.scalars(select(FileRecord))
    by_type: Counter[str] = Counter()
    by_type_bytes: Counter[str] = Counter()
    listed: list[FileRecord] = list(rows)
    for r in listed:
        k = _classify(r.mime_type or "")
        by_type[k] += 1
        by_type_bytes[k] += int(r.size_bytes or 0)

    largest = sorted(listed, key=lambda r: r.size_bytes or 0, reverse=True)[:10]
    oldest = sorted(listed, key=lambda r: r.created_at)[:10]

    return StatsOut(
        total_files=int(total_files), total_bytes=int(total_bytes),
        by_type=dict(by_type), by_type_bytes=dict(by_type_bytes),
        largest=[
            {"id": r.id, "name": r.original_name, "size": r.size_bytes,
             "mime": r.mime_type}
            for r in largest
        ],
        oldest=[
            {"id": r.id, "name": r.original_name, "created_at": r.created_at.isoformat()}
            for r in oldest
        ],
    )


# ── Preview / Download / Delete ────────────────────────────

async def _resolve_record(db: AsyncSession, file_id: str) -> FileRecord:
    rec = await db.get(FileRecord, file_id)
    if not rec:
        raise HTTPException(status_code=404, detail="File not found.")
    return rec


@router.get("/preview/{file_id}")
async def preview(
    file_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_role("admin")),
):
    rec = await _resolve_record(db, file_id)
    p = Path(rec.storage_path)
    if not p.exists():
        raise HTTPException(status_code=410, detail="File missing on disk.")
    kind = _classify(rec.mime_type)
    if kind not in ("image", "text", "pdf"):
        raise HTTPException(status_code=415, detail=f"Cannot preview {kind} type.")
    if kind == "text":
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            return Response(content=content[:64 * 1024], media_type="text/plain")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
    return FileResponse(p, media_type=rec.mime_type)


@router.get("/download/{file_id}")
async def download(
    file_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_role("admin")),
):
    rec = await _resolve_record(db, file_id)
    p = Path(rec.storage_path)
    if not p.exists():
        raise HTTPException(status_code=410, detail="File missing on disk.")
    audit_log("admin.file_download", user_id=user_id, success=True,
              details={"file_id": file_id, "original_name": rec.original_name})
    return FileResponse(
        p, media_type=rec.mime_type, filename=rec.original_name,
    )


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_role("admin")),
) -> None:
    rec = await _resolve_record(db, file_id)
    storage = Path(rec.storage_path)
    try:
        if storage.exists():
            storage.unlink()
    except OSError as exc:                                         # pragma: no cover
        logger.error("file_delete_disk_failed", file_id=file_id, error=str(exc))
    await db.delete(rec)
    await db.commit()
    audit_log("admin.file_deleted", user_id=user_id, success=True,
              details={"file_id": file_id, "size": rec.size_bytes})


# ── Cleanup wizard ─────────────────────────────────────────

class CleanupIn(BaseModel):
    older_than_days: int = 0
    types: Optional[list[str]] = None   # filter on _classify(mime)
    min_size_mb: int = 0
    dry_run: bool = True


class CleanupOut(BaseModel):
    matched: int
    deleted: int
    freed_bytes: int
    dry_run: bool


@router.post("/cleanup", response_model=CleanupOut)
async def cleanup(
    body: CleanupIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_role("admin")),
) -> CleanupOut:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, body.older_than_days))
    rows = await db.scalars(select(FileRecord))
    rows_list: list[FileRecord] = list(rows)

    def _match(r: FileRecord) -> bool:
        if body.older_than_days and r.created_at > cutoff:
            return False
        if body.min_size_mb and (r.size_bytes or 0) < body.min_size_mb * 1024 * 1024:
            return False
        if body.types and _classify(r.mime_type or "") not in body.types:
            return False
        return True

    matches = [r for r in rows_list if _match(r)]
    if body.dry_run:
        return CleanupOut(
            matched=len(matches), deleted=0,
            freed_bytes=sum(r.size_bytes or 0 for r in matches),
            dry_run=True,
        )

    freed = 0
    for r in matches:
        p = Path(r.storage_path)
        try:
            if p.exists():
                p.unlink()
        except OSError:
            continue
        freed += r.size_bytes or 0
        await db.delete(r)
    await db.commit()
    audit_log("admin.file_cleanup", user_id=user_id, success=True,
              details={"deleted": len(matches), "freed_bytes": freed})
    return CleanupOut(
        matched=len(matches), deleted=len(matches), freed_bytes=freed, dry_run=False,
    )


# ── Live upload monitor ────────────────────────────────────

@router.get("/upload-monitor")
async def upload_monitor(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_role("admin")),
):
    """Snapshot of in-progress chunked uploads from the resumable system."""
    out: list[dict[str, Any]] = []
    try:
        from app.models.upload_session import UploadSession
        rows = await db.scalars(
            select(UploadSession).where(
                getattr(UploadSession, "status", "active") != "completed"
            ).order_by(UploadSession.created_at.desc()).limit(200)
        )
        for r in rows:
            out.append({
                "id": getattr(r, "id", None),
                "user_id": getattr(r, "user_id", None),
                "original_name": getattr(r, "original_name", None),
                "size_bytes": getattr(r, "size_bytes", None),
                "received_bytes": getattr(r, "received_bytes", None),
                "status": getattr(r, "status", None),
                "created_at": (
                    getattr(r, "created_at", None).isoformat()
                    if getattr(r, "created_at", None) else None
                ),
            })
    except Exception as exc:
        logger.warning("upload_monitor_error", error=str(exc))
    return {"items": out, "count": len(out)}


# ── Vault bridge ───────────────────────────────────────────

@router.get("/vault")
async def list_vault(
    workspace_id: Optional[str] = Query(None),
    user_id: str = Depends(require_role("admin")),
):
    status_payload = await vault_bridge.get_status()
    if not status_payload.available:
        return {"available": False, "detail": status_payload.detail, "items": []}
    items = await vault_bridge.list_encrypted_files(workspace_id)
    return {"available": True, "version": status_payload.version, "items": items}


@router.get("/vault/preview/{vault_id}")
async def vault_preview(
    vault_id: str,
    user_id: str = Depends(require_role("admin")),
):
    data = await vault_bridge.decrypt_for_preview(vault_id, requester_user_id=user_id)
    if data is None:
        raise HTTPException(status_code=404, detail="vault item not found")
    return Response(content=data, media_type="application/octet-stream")
