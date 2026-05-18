"""
Phase 6 / Module AB — consent tracking.

Pure business logic; route layer wraps `record_consent` /
`get_consent_status` / `requires_renewal`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.compliance import VALID_CONSENT_TYPES, ConsentRecord

logger = get_logger(__name__)


async def record_consent(
    user_id: str,
    consent_type: str,
    granted: bool,
    *,
    version: str = "1.0",
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> ConsentRecord:
    if consent_type not in VALID_CONSENT_TYPES:
        raise ValueError(f"invalid consent type: {consent_type}")
    async with async_session_factory() as db:
        rec = ConsentRecord(
            id=uuid.uuid4().hex,
            user_id=user_id,
            consent_type=consent_type,
            granted=granted,
            version=version,
            ip_address=ip_address,
            user_agent=user_agent,
            granted_at=datetime.now(timezone.utc),
            revoked_at=None if granted else datetime.now(timezone.utc),
        )
        db.add(rec)
        await db.commit()
        return rec


async def get_consent_status(user_id: str) -> Dict[str, Dict[str, Any]]:
    """Return the most recent record per consent_type."""
    out: Dict[str, Dict[str, Any]] = {}
    async with async_session_factory() as db:
        for ctype in VALID_CONSENT_TYPES:
            row = (await db.execute(
                select(ConsentRecord)
                .where(ConsentRecord.user_id == user_id,
                       ConsentRecord.consent_type == ctype)
                .order_by(desc(ConsentRecord.granted_at)).limit(1)
            )).scalar_one_or_none()
            if row is None:
                out[ctype] = {"granted": False, "version": None, "ts": None}
            else:
                out[ctype] = {
                    "granted": bool(row.granted),
                    "version": row.version,
                    "ts": row.granted_at.isoformat() if row.granted_at else None,
                }
    return out


async def requires_renewal(user_id: str, current_version: str) -> List[str]:
    """Return consent types whose latest record is older than ``current_version``."""
    out: List[str] = []
    status = await get_consent_status(user_id)
    for ctype, info in status.items():
        if not info["granted"] or (info["version"] or "0") < current_version:
            out.append(ctype)
    return out


async def list_user_history(user_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(ConsentRecord)
            .where(ConsentRecord.user_id == user_id)
            .order_by(desc(ConsentRecord.granted_at)).limit(limit)
        )).scalars().all()
    return [
        {
            "id": r.id, "type": r.consent_type, "granted": r.granted,
            "version": r.version,
            "granted_at": r.granted_at.isoformat() if r.granted_at else None,
            "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
            "ip_address": r.ip_address, "user_agent": r.user_agent,
        }
        for r in rows
    ]
