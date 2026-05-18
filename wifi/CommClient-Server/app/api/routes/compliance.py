"""
Phase 6 / Module AB — user-facing compliance endpoints.

Self-service GDPR primitives the end user can call without admin perms:

  POST /api/me/data/export
  GET  /api/me/data/export/{id}
  GET  /api/me/data/export/{id}/download
  POST /api/me/data/delete
  POST /api/me/data/delete/confirm
  GET  /api/me/consents
  POST /api/me/consents
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.compliance import (
    VALID_CONSENT_TYPES,
    DataDeletionRequest,
    DataExportRequest,
)
from app.services.compliance import consent_manager, data_deletion, data_export

logger = get_logger(__name__)
router = APIRouter(prefix="/api/me", tags=["compliance-self-service"])


# ── exports ─────────────────────────────────────────────────────


class ExportStatus(BaseModel):
    id: str
    status: str
    requested_at: Optional[str]
    completed_at: Optional[str]
    size_bytes: int
    sha256: Optional[str]
    expires_at: Optional[str]
    downloaded: bool


@router.post("/data/export")
async def request_data_export(
    user_id: str = Depends(get_current_user_id),
):
    rid = await data_export.request_export(user_id)
    # fulfill in background
    asyncio.create_task(data_export.fulfill_export(rid))
    audit_log("compliance.export_requested", user_id=user_id, success=True,
              details={"request_id": rid})
    return {"request_id": rid, "status": "pending"}


@router.get("/data/export/{request_id}", response_model=ExportStatus)
async def get_export_status(
    request_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    req = (await db.execute(
        select(DataExportRequest).where(DataExportRequest.id == request_id)
    )).scalar_one_or_none()
    if req is None or req.user_id != user_id:
        raise HTTPException(404, detail="export request not found")
    return ExportStatus(
        id=req.id, status=req.status,
        requested_at=req.requested_at.isoformat() if req.requested_at else None,
        completed_at=req.completed_at.isoformat() if req.completed_at else None,
        size_bytes=req.size_bytes, sha256=req.sha256,
        expires_at=req.expires_at.isoformat() if req.expires_at else None,
        downloaded=req.downloaded,
    )


@router.get("/data/export/{request_id}/download")
async def download_export(
    request_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    req = (await db.execute(
        select(DataExportRequest).where(DataExportRequest.id == request_id)
    )).scalar_one_or_none()
    if req is None or req.user_id != user_id:
        raise HTTPException(404, detail="export request not found")
    if req.status != "ready":
        raise HTTPException(409, detail=f"export not ready (status={req.status})")
    if not req.file_path or not Path(req.file_path).exists():
        raise HTTPException(410, detail="archive missing")
    req.downloaded = True
    await db.commit()
    audit_log("compliance.export_downloaded", user_id=user_id, success=True,
              details={"request_id": request_id})
    return FileResponse(
        req.file_path,
        media_type="application/zip",
        filename=f"data_export_{request_id}.zip",
    )


# ── deletion ────────────────────────────────────────────────────


class DeletionRequestBody(BaseModel):
    delay_hours: int = Field(default=24, ge=0, le=720)


class DeletionConfirmBody(BaseModel):
    request_id: str
    confirmation_token: str


@router.post("/data/delete")
async def request_data_deletion(
    body: DeletionRequestBody = DeletionRequestBody(),
    user_id: str = Depends(get_current_user_id),
):
    result = await data_deletion.request_deletion(
        user_id, dry_run=True, scheduled_delay_hours=body.delay_hours,
    )
    audit_log("compliance.deletion_requested", user_id=user_id, success=True,
              details={"request_id": result["request_id"]})
    return result


@router.post("/data/delete/confirm")
async def confirm_data_deletion(
    body: DeletionConfirmBody,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    req = (await db.execute(
        select(DataDeletionRequest).where(DataDeletionRequest.id == body.request_id)
    )).scalar_one_or_none()
    if req is None or req.user_id != user_id:
        raise HTTPException(404, detail="deletion request not found")
    try:
        result = await data_deletion.execute_deletion(
            body.request_id, body.confirmation_token,
        )
    except PermissionError as e:
        raise HTTPException(403, detail=str(e))
    return result


# ── consents ────────────────────────────────────────────────────


class ConsentIn(BaseModel):
    consent_type: str = Field(..., description="|".join(VALID_CONSENT_TYPES))
    granted: bool
    version: str = "1.0"


@router.get("/consents")
async def get_consents(
    user_id: str = Depends(get_current_user_id),
):
    return await consent_manager.get_consent_status(user_id)


@router.post("/consents")
async def set_consent(
    body: ConsentIn,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    rec = await consent_manager.record_consent(
        user_id,
        body.consent_type,
        body.granted,
        version=body.version,
        ip_address=ip,
        user_agent=ua,
    )
    audit_log("compliance.consent_recorded", user_id=user_id, success=True,
              details={"type": body.consent_type, "granted": body.granted})
    return {"id": rec.id, "type": rec.consent_type, "granted": rec.granted,
            "version": rec.version}


@router.get("/consents/history")
async def consent_history(
    user_id: str = Depends(get_current_user_id),
):
    return await consent_manager.list_user_history(user_id)
