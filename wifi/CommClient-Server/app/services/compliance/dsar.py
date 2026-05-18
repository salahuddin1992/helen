"""
DSARService — GDPR Article 15 (Right of Access) / Article 20 (Portability).

Builds on the existing ``data_export.build_export_archive`` to produce a
subject-data ZIP, and adds:
  * identity verification gate
  * deadline tracking (default 30 days)
  * response-letter templates
  * full lifecycle: pending → identity_verified → running → fulfilled
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.compliance_dsar import (
    VALID_DSAR_STATUSES,
    VALID_DSAR_TYPES,
    DSARRequest,
)
from app.services.compliance import data_export

logger = get_logger(__name__)


_TEMPLATES: Dict[str, str] = {
    "access":
        "Dear {name},\n\n"
        "We have completed your access request (DSAR #{id}) under "
        "GDPR Article 15. A signed archive containing every piece of "
        "personal data we hold about you is attached.\n\n"
        "Reference: {id}\nReceived: {received}\nFulfilled: {fulfilled}\n\n"
        "Regards,\nData Protection Office",
    "portability":
        "Dear {name},\n\n"
        "Per GDPR Article 20 we provide your personal data in a "
        "structured, commonly used, machine-readable format. The "
        "attached archive contains JSON dumps you can move to any "
        "other controller.\n\n"
        "Reference: {id}\nReceived: {received}\nFulfilled: {fulfilled}\n\n"
        "Regards,\nData Protection Office",
    "rectification":
        "Dear {name},\n\n"
        "Your rectification request (DSAR #{id}) has been processed. "
        "Please review the attached confirmation pack.\n\n"
        "Reference: {id}\nReceived: {received}\nFulfilled: {fulfilled}\n\n"
        "Regards,\nData Protection Office",
}


class DSARService:
    async def list_requests(
        self, db: AsyncSession, *,
        status: Optional[str] = None, overdue: bool = False,
        limit: int = 100, offset: int = 0,
    ) -> List[DSARRequest]:
        q = select(DSARRequest)
        if status:
            q = q.where(DSARRequest.status == status)
        if overdue:
            now = datetime.now(timezone.utc)
            q = q.where(DSARRequest.deadline_at < now,
                        DSARRequest.status.notin_(("fulfilled", "rejected", "expired")))
        q = q.order_by(desc(DSARRequest.received_at)).offset(offset).limit(limit)
        return list((await db.execute(q)).scalars().all())

    async def get(self, db: AsyncSession, dsar_id: str) -> Optional[DSARRequest]:
        return (await db.execute(
            select(DSARRequest).where(DSARRequest.id == dsar_id)
        )).scalar_one_or_none()

    async def create(
        self, db: AsyncSession, *,
        subject_id: str,
        subject_email: Optional[str] = None,
        subject_name: Optional[str] = None,
        request_type: str = "access",
        identity_verified: bool = False,
        identity_proof: Optional[Dict[str, Any]] = None,
        scope: Optional[Dict[str, Any]] = None,
        deadline_days: int = 30,
        actor_id: str = "system",
    ) -> DSARRequest:
        if request_type not in VALID_DSAR_TYPES:
            raise ValueError(f"request_type must be one of {VALID_DSAR_TYPES}")
        r = DSARRequest(
            id=uuid.uuid4().hex,
            subject_id=subject_id,
            subject_email=subject_email,
            subject_name=subject_name,
            request_type=request_type,
            identity_verified=bool(identity_verified),
            identity_proof=identity_proof,
            scope=scope or {},
            status="identity_verified" if identity_verified else "pending",
            deadline_at=datetime.now(timezone.utc) + timedelta(days=max(1, int(deadline_days))),
            created_by=actor_id,
        )
        db.add(r)
        await db.commit()
        audit_log("compliance.dsar_created", user_id=actor_id, success=True,
                  details={"dsar_id": r.id, "subject_id": subject_id,
                           "request_type": request_type})
        return r

    async def verify_identity(
        self, db: AsyncSession, dsar_id: str, *,
        proof: Dict[str, Any], actor_id: str,
    ) -> DSARRequest:
        r = await self.get(db, dsar_id)
        if r is None:
            raise LookupError(dsar_id)
        if r.status == "fulfilled":
            return r
        r.identity_verified = True
        r.identity_proof = proof
        if r.status == "pending":
            r.status = "identity_verified"
        await db.commit()
        audit_log("compliance.dsar_identity_verified", user_id=actor_id, success=True,
                  details={"dsar_id": r.id})
        return r

    async def fulfill(
        self, db: AsyncSession, dsar_id: str, *,
        redact_pii: bool = False,
        response_template: Optional[str] = None,
        actor_id: str = "system",
    ) -> Dict[str, Any]:
        r = await self.get(db, dsar_id)
        if r is None:
            raise LookupError(dsar_id)
        if not r.identity_verified:
            raise PermissionError("identity not verified")
        if r.status == "fulfilled":
            return {"dsar_id": r.id, "status": "already_fulfilled",
                    "file_path": r.file_path}

        r.status = "running"
        await db.commit()

        try:
            artifact = await data_export.build_export_archive(
                user_id=r.subject_id, request_id=r.id,
            )
        except Exception as e:
            r.status = "failed"
            r.error_message = str(e)[:1024]
            await db.commit()
            raise

        letter = _TEMPLATES.get(response_template or r.request_type, _TEMPLATES["access"])
        rendered = letter.format(
            name=r.subject_name or r.subject_id,
            id=r.id,
            received=(r.received_at.isoformat() if r.received_at else "—"),
            fulfilled=datetime.now(timezone.utc).isoformat(),
        )

        r.file_path = artifact["path"]
        r.sha256 = artifact["sha256"]
        r.size_bytes = artifact["size_bytes"]
        r.fulfilled_at = datetime.now(timezone.utc)
        r.response_letter = rendered
        r.status = "fulfilled"
        await db.commit()
        audit_log("compliance.dsar_fulfilled", user_id=actor_id, success=True,
                  details={"dsar_id": r.id, "sha256": artifact["sha256"],
                           "size": artifact["size_bytes"]})
        return {
            "dsar_id": r.id,
            "status": "fulfilled",
            "file_path": r.file_path,
            "sha256": r.sha256,
            "size_bytes": r.size_bytes,
            "response_letter": rendered,
        }


dsar_service = DSARService()
