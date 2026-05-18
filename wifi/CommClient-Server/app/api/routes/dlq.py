"""
Admin REST endpoints for the messaging Dead-Letter Queue.

Every endpoint is gated by ``require_role("admin")`` and emits an audit
log row via :func:`app.core.audit.audit_log`.

Endpoints
---------
  GET    /api/admin/dlq                 — Paginated listing (status, kind filters)
  GET    /api/admin/dlq/stats           — Aggregate counts by status / kind
  GET    /api/admin/dlq/{entry_id}      — Entry detail
  POST   /api/admin/dlq/{entry_id}/replay    — Force immediate replay
  POST   /api/admin/dlq/{entry_id}/abandon   — Mark abandoned (with operator note)
  POST   /api/admin/dlq/reaper/tick     — Manually trigger one reaper pass
  POST   /api/admin/dlq/purge-replayed  — Bulk delete replayed rows older than N days
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.core.security_utils import require_role
from app.models.message_dead_letter import MessageDeadLetter
from app.services.dead_letter_service import DeadLetterService, SUPPORTED_KINDS

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/dlq", tags=["admin-dlq"])


_VALID_STATUSES = {"pending", "replaying", "replayed", "abandoned"}


class AbandonRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2048)


def _serialize(row: MessageDeadLetter, *, include_payload: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": row.id,
        "message_id": row.message_id,
        "channel_id": row.channel_id,
        "sender_id": row.sender_id,
        "kind": row.kind,
        "reason": row.reason,
        "error": row.error,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else None,
        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "operator_note": row.operator_note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_payload:
        payload: Any = None
        if row.payload_json:
            try:
                payload = json.loads(row.payload_json)
            except Exception:
                payload = {"_raw": row.payload_json[:2048]}
        d["payload"] = payload
    return d


# ── Listing ─────────────────────────────────────────────────────────────────


@router.get("")
async def list_dlq(
    status_filter: str | None = Query(default=None, alias="status"),
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Paginated DLQ listing. Filter by status (``pending|replaying|replayed|
    abandoned``) and/or ``kind`` (``fanout|webhook|push|scheduled|...``).
    """
    if status_filter and status_filter not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {sorted(_VALID_STATUSES)}",
        )
    if kind and kind not in SUPPORTED_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid kind. Must be one of: {sorted(SUPPORTED_KINDS)}",
        )

    rows, total = await DeadLetterService.list_entries(
        db, status=status_filter, kind=kind, limit=limit, offset=offset
    )
    audit_log(
        "admin.dlq_listed",
        user_id=user_id,
        success=True,
        details={
            "status": status_filter,
            "kind": kind,
            "limit": limit,
            "offset": offset,
            "returned": len(rows),
        },
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [_serialize(r) for r in rows],
    }


@router.get("/stats")
async def dlq_stats(
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate counts grouped by status and kind."""
    stats = await DeadLetterService.stats(db)
    audit_log("admin.dlq_stats", user_id=user_id, success=True)
    return stats


@router.get("/{entry_id}")
async def get_dlq_entry(
    entry_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Single entry detail — includes the full deserialized payload."""
    row = await DeadLetterService.get_entry(db, entry_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
    audit_log(
        "admin.dlq_viewed",
        user_id=user_id,
        success=True,
        details={"entry_id": entry_id},
    )
    return _serialize(row, include_payload=True)


# ── Mutations ───────────────────────────────────────────────────────────────


@router.post("/{entry_id}/replay")
async def replay_dlq_entry(
    entry_id: str,
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Force one replay attempt. Status is updated based on the outcome."""
    row = await DeadLetterService.replay_entry(db, entry_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
    audit_log(
        "admin.dlq_replayed",
        user_id=user_id,
        success=row.status == "replayed",
        details={
            "entry_id": entry_id,
            "kind": row.kind,
            "status": row.status,
            "attempts": row.attempt_count,
        },
    )
    return _serialize(row, include_payload=True)


@router.post("/{entry_id}/abandon")
async def abandon_dlq_entry(
    entry_id: str,
    body: AbandonRequest | None = Body(default=None),
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Mark an entry as abandoned. Replay will no longer be retried."""
    note = body.note if body else None
    row = await DeadLetterService.abandon(db, entry_id, note=note)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
    audit_log(
        "admin.dlq_abandoned",
        user_id=user_id,
        success=True,
        details={"entry_id": entry_id, "note": note},
    )
    return _serialize(row, include_payload=True)


# ── Maintenance ─────────────────────────────────────────────────────────────


@router.post("/reaper/tick")
async def force_reaper_tick(
    batch: int = Query(default=50, ge=1, le=500),
    user_id: str = Depends(require_role("admin")),
):
    """Manually invoke one reaper tick (useful for tests / operators)."""
    replayed = await DeadLetterService._reaper_tick(batch=batch)
    audit_log(
        "admin.dlq_reaper_tick",
        user_id=user_id,
        success=True,
        details={"batch": batch, "replayed": replayed},
    )
    return {"replayed": replayed, "batch": batch}


@router.post("/purge-replayed")
async def purge_replayed(
    older_than_days: int = Query(default=30, ge=1, le=3650),
    user_id: str = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete DLQ rows whose status is ``replayed`` and whose ``resolved_at``
    is older than ``older_than_days``.

    ``abandoned`` rows are deliberately preserved — they represent
    unresolved failures that may still be audited.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    result = await db.execute(
        delete(MessageDeadLetter)
        .where(
            MessageDeadLetter.status == "replayed",
            MessageDeadLetter.resolved_at <= cutoff,
        )
    )
    await db.commit()
    deleted = int(result.rowcount or 0)
    audit_log(
        "admin.dlq_purged_replayed",
        user_id=user_id,
        success=True,
        details={"older_than_days": older_than_days, "deleted": deleted},
    )
    return {"deleted": deleted, "older_than_days": older_than_days}
