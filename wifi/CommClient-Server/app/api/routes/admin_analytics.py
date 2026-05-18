"""
Phase 7 / Module AI — admin analytics endpoints.

Mounted under ``/api/admin/analytics``. Requires ``analytics.admin``.
Includes a WebSocket live event stream for observability tooling.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.security import decode_token
from app.core.logging import get_logger
from app.models.analytics import AnalyticsEvent, SavedQuery
from app.services.analytics.event_ingester import (
    buffer_size,
    force_flush,
)
from app.services.analytics.warehouse_export import (
    export_window,
    schedule_daily_export,
)
from app.services.rbac.enforcer import (
    require_permission,
    user_has_permission,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin/analytics", tags=["admin-analytics"])

_PERM = "analytics.admin"


# ───────────────────────────────────────────────────────────────────────
# Stats
# ───────────────────────────────────────────────────────────────────────


@router.get("/stats")
async def stats(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    total = (await db.execute(
        select(func.count(AnalyticsEvent.id))
    )).scalar_one() or 0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
    rate = (await db.execute(
        select(func.count(AnalyticsEvent.id))
        .where(AnalyticsEvent.ingested_at >= cutoff)
    )).scalar_one() or 0
    by_workspace = (await db.execute(
        select(AnalyticsEvent.workspace_id, func.count(AnalyticsEvent.id))
        .group_by(AnalyticsEvent.workspace_id)
        .order_by(desc(func.count(AnalyticsEvent.id)))
        .limit(20)
    )).all()
    return {
        "total_events": total,
        "events_per_minute": rate,
        "buffer_size": buffer_size(),
        "top_workspaces": [
            {"workspace_id": w, "events": int(c or 0)}
            for w, c in by_workspace
        ],
    }


@router.post("/flush")
async def flush(
    _user: str = Depends(require_permission(_PERM)),
):
    written = await force_flush()
    return {"written": written}


# ───────────────────────────────────────────────────────────────────────
# Warehouse exports
# ───────────────────────────────────────────────────────────────────────


class ExportIn(BaseModel):
    backend: str = Field(..., pattern="^(bigquery|snowflake|s3_parquet)$")
    config: dict[str, Any] = Field(default_factory=dict)
    workspace_id: Optional[str] = None
    since: Optional[datetime] = None
    until: Optional[datetime] = None


class ScheduleIn(BaseModel):
    backend: str = Field(..., pattern="^(bigquery|snowflake|s3_parquet)$")
    config: dict[str, Any] = Field(default_factory=dict)
    workspace_id: Optional[str] = None
    interval_seconds: int = 86_400


_running_exports: dict[str, asyncio.Task] = {}


@router.post("/warehouse-exports")
async def run_export(
    body: ExportIn,
    user_id: str = Depends(require_permission(_PERM)),
):
    now = datetime.now(timezone.utc)
    since = body.since or now - timedelta(days=1)
    until = body.until or now
    result = await export_window(
        backend=body.backend, config=body.config,
        workspace_id=body.workspace_id,
        since=since, until=until,
    )
    audit_log("analytics.warehouse.export", user_id=user_id, success=result.ok,
              details={"backend": body.backend, "rows": result.rows,
                       "error": result.error})
    return {
        "backend": result.backend, "ok": result.ok,
        "rows": result.rows, "bytes": result.bytes,
        "path": result.path, "error": result.error,
    }


@router.post("/warehouse-exports/schedule")
async def schedule_export(
    body: ScheduleIn,
    user_id: str = Depends(require_permission(_PERM)),
):
    key = f"{body.backend}:{body.workspace_id or '*'}"
    if key in _running_exports and not _running_exports[key].done():
        return {"ok": True, "already_scheduled": True}
    task = asyncio.create_task(schedule_daily_export(
        backend=body.backend, config=body.config,
        workspace_id=body.workspace_id,
        interval_seconds=body.interval_seconds,
    ))
    _running_exports[key] = task
    audit_log("analytics.warehouse.scheduled", user_id=user_id, success=True,
              details={"key": key, "interval": body.interval_seconds})
    return {"ok": True, "key": key}


@router.delete("/warehouse-exports/schedule/{key}")
async def stop_scheduled_export(
    key: str,
    _user: str = Depends(require_permission(_PERM)),
):
    task = _running_exports.pop(key, None)
    if task and not task.done():
        task.cancel()
    return {"ok": True, "key": key}


# ───────────────────────────────────────────────────────────────────────
# Saved queries
# ───────────────────────────────────────────────────────────────────────


@router.get("/queries/saved")
async def admin_saved_queries(
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(SavedQuery).order_by(desc(SavedQuery.created_at)).limit(500)
    )).scalars().all()
    return {"items": [
        {
            "id": q.id, "workspace_id": q.workspace_id, "name": q.name,
            "query_dsl": dict(q.query_dsl or {}),
            "created_at": q.created_at.isoformat() if q.created_at else None,
        } for q in rows
    ]}


# ───────────────────────────────────────────────────────────────────────
# Live event stream (WebSocket)
# ───────────────────────────────────────────────────────────────────────


@router.websocket("/events-stream")
async def events_stream(
    ws: WebSocket,
    token: str = Query(...),
    workspace_id: Optional[str] = Query(None),
):
    # Auth check (WS can't easily use Depends for permissions)
    try:
        payload = decode_token(token)
    except Exception:                                                   # noqa: BLE001
        await ws.close(code=4401)
        return
    if payload.get("type") != "access":
        await ws.close(code=4401)
        return
    user_id = payload.get("sub")
    if not user_id:
        await ws.close(code=4401)
        return
    from app.db.session import async_session_factory
    async with async_session_factory() as db:
        if not await user_has_permission(db, user_id, _PERM):
            await ws.close(code=4403)
            return

    await ws.accept()
    last_seen_id = ""
    try:
        while True:
            async with async_session_factory() as db:
                q = select(AnalyticsEvent).order_by(
                    desc(AnalyticsEvent.ingested_at),
                ).limit(50)
                if workspace_id:
                    q = q.where(AnalyticsEvent.workspace_id == workspace_id)
                if last_seen_id:
                    q = q.where(AnalyticsEvent.id != last_seen_id)
                rows = (await db.execute(q)).scalars().all()
            for ev in reversed(rows):
                if ev.id == last_seen_id:
                    continue
                await ws.send_text(json.dumps({
                    "id": ev.id, "workspace_id": ev.workspace_id,
                    "user_id": ev.user_id, "event": ev.event_name,
                    "properties": ev.properties,
                    "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                }))
                last_seen_id = ev.id
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
    except Exception as e:                                              # noqa: BLE001
        logger.warning("analytics.ws.error %s", e)
        try:
            await ws.close()
        except Exception:                                               # noqa: BLE001
            pass
