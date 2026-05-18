"""
Phase 7 / Module AI — user-facing analytics endpoints.

Mounted under ``/api/analytics``. Tenant-scoped via the caller's
workspace membership.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.analytics import (
    Cohort,
    Dashboard,
    Funnel,
    SavedQuery,
    Widget,
)
from app.models.workspace import WorkspaceMember
from app.services.analytics.cohort_engine import compute_cohort
from app.services.analytics.embedded_reports import (
    embed_url,
    sign_embed_token,
    verify_embed_token,
)
from app.services.analytics.event_ingester import track, track_batch
from app.services.analytics.funnel_engine import compute_funnel
from app.services.analytics.query_engine import run_query, top_events

logger = get_logger(__name__)
router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────


async def _ws(db: AsyncSession, user_id: str) -> str:
    wid = (await db.execute(
        select(WorkspaceMember.workspace_id).where(
            WorkspaceMember.user_id == user_id,
        ).limit(1)
    )).scalar_one_or_none()
    if not wid:
        raise HTTPException(404, "no-workspace")
    return wid


# ───────────────────────────────────────────────────────────────────────
# Schemas
# ───────────────────────────────────────────────────────────────────────


class TrackIn(BaseModel):
    event: str = Field(..., min_length=1, max_length=128)
    properties: dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = None
    occurred_at: Optional[str] = None
    user_id: Optional[str] = None


class BatchIn(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)


class DashboardIn(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    layout: dict[str, Any] = Field(default_factory=dict)
    shared: bool = False


class WidgetIn(BaseModel):
    name: str
    kind: str
    config: dict[str, Any] = Field(default_factory=dict)
    position: dict[str, Any] = Field(default_factory=dict)


class QueryIn(BaseModel):
    dsl: dict[str, Any] = Field(default_factory=dict)
    save_as: Optional[str] = None


class CohortIn(BaseModel):
    name: str
    definition: dict[str, Any] = Field(default_factory=dict)


class FunnelIn(BaseModel):
    name: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    conversion_window_days: int = 7


# ───────────────────────────────────────────────────────────────────────
# Ingest
# ───────────────────────────────────────────────────────────────────────


@router.post("/track")
async def track_one(
    body: TrackIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    track(
        workspace_id=wid,
        event_name=body.event,
        user_id=body.user_id or user_id,
        properties=body.properties,
        session_id=body.session_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    return {"queued": True}


@router.post("/batch")
async def track_many(
    body: BatchIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    accepted = track_batch(
        body.events, workspace_id=wid, user_id=user_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    return {"accepted": accepted}


# ───────────────────────────────────────────────────────────────────────
# Dashboards / Widgets
# ───────────────────────────────────────────────────────────────────────


@router.get("/dashboards")
async def list_dashboards(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    rows = (await db.execute(
        select(Dashboard).where(Dashboard.workspace_id == wid)
        .order_by(desc(Dashboard.created_at))
    )).scalars().all()
    return {"items": [
        {
            "id": d.id, "name": d.name, "slug": d.slug,
            "shared": d.shared, "created_at": d.created_at.isoformat(),
        } for d in rows
    ]}


@router.post("/dashboards")
async def create_dashboard(
    body: DashboardIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    slug = body.slug or body.name.lower().replace(" ", "-")[:64]
    d = Dashboard(
        workspace_id=wid, name=body.name, slug=slug,
        description=body.description, layout=body.layout,
        shared=body.shared, created_by=user_id,
    )
    db.add(d)
    await db.commit()
    return {"id": d.id, "slug": d.slug}


@router.get("/dashboards/{dashboard_id}")
async def get_dashboard(
    dashboard_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    d = (await db.execute(
        select(Dashboard).where(
            Dashboard.id == dashboard_id, Dashboard.workspace_id == wid,
        )
    )).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "dashboard-not-found")
    return {
        "id": d.id, "name": d.name, "slug": d.slug,
        "description": d.description, "layout": dict(d.layout or {}),
        "shared": d.shared,
        "widgets": [
            {
                "id": w.id, "name": w.name, "kind": w.kind,
                "config": dict(w.config or {}), "position": dict(w.position or {}),
            } for w in (d.widgets or [])
        ],
    }


@router.patch("/dashboards/{dashboard_id}")
async def update_dashboard(
    dashboard_id: str,
    body: DashboardIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    d = (await db.execute(
        select(Dashboard).where(
            Dashboard.id == dashboard_id, Dashboard.workspace_id == wid,
        )
    )).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "dashboard-not-found")
    d.name = body.name
    if body.slug:
        d.slug = body.slug
    d.description = body.description
    d.layout = body.layout
    d.shared = body.shared
    await db.commit()
    return {"ok": True}


@router.delete("/dashboards/{dashboard_id}")
async def delete_dashboard(
    dashboard_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    d = (await db.execute(
        select(Dashboard).where(
            Dashboard.id == dashboard_id, Dashboard.workspace_id == wid,
        )
    )).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "dashboard-not-found")
    await db.delete(d)
    await db.commit()
    return {"ok": True}


@router.post("/dashboards/{dashboard_id}/widgets")
async def add_widget(
    dashboard_id: str,
    body: WidgetIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    d = (await db.execute(
        select(Dashboard).where(
            Dashboard.id == dashboard_id, Dashboard.workspace_id == wid,
        )
    )).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "dashboard-not-found")
    w = Widget(
        dashboard_id=d.id, name=body.name, kind=body.kind,
        config=body.config, position=body.position,
    )
    db.add(w)
    await db.commit()
    return {"id": w.id}


@router.patch("/widgets/{widget_id}")
async def update_widget(
    widget_id: str,
    body: WidgetIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    row = (await db.execute(
        select(Widget, Dashboard).join(
            Dashboard, Dashboard.id == Widget.dashboard_id,
        ).where(Widget.id == widget_id, Dashboard.workspace_id == wid)
    )).first()
    if not row:
        raise HTTPException(404, "widget-not-found")
    w, _ = row
    w.name = body.name
    w.kind = body.kind
    w.config = body.config
    w.position = body.position
    await db.commit()
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────────
# Query / Saved Queries
# ───────────────────────────────────────────────────────────────────────


@router.post("/query")
async def query(
    body: QueryIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    dsl = dict(body.dsl)
    dsl["workspace_id"] = wid
    try:
        result = await run_query(db, dsl)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.save_as:
        db.add(SavedQuery(
            workspace_id=wid, name=body.save_as,
            query_dsl=body.dsl, created_by=user_id,
        ))
        await db.commit()
    return {"items": result}


@router.get("/saved-queries")
async def list_saved_queries(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    rows = (await db.execute(
        select(SavedQuery).where(SavedQuery.workspace_id == wid)
        .order_by(desc(SavedQuery.created_at))
    )).scalars().all()
    return {"items": [
        {
            "id": q.id, "name": q.name, "query_dsl": dict(q.query_dsl or {}),
            "last_run_at": q.last_run_at.isoformat() if q.last_run_at else None,
        } for q in rows
    ]}


@router.get("/top-events")
async def top(
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    return {"items": await top_events(db, wid, days=days, limit=limit)}


# ───────────────────────────────────────────────────────────────────────
# Cohorts
# ───────────────────────────────────────────────────────────────────────


@router.post("/cohorts")
async def create_cohort(
    body: CohortIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    c = Cohort(workspace_id=wid, name=body.name, definition=body.definition)
    db.add(c)
    await db.flush()
    snap = await compute_cohort(db, c)
    return {"id": c.id, "snapshot": snap}


@router.get("/cohorts/{cohort_id}/results")
async def cohort_results(
    cohort_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    c = (await db.execute(
        select(Cohort).where(Cohort.id == cohort_id, Cohort.workspace_id == wid)
    )).scalar_one_or_none()
    if not c:
        raise HTTPException(404, "cohort-not-found")
    return {"snapshot": dict(c.retention_snapshot or {}),
            "user_count": c.user_count,
            "last_computed_at": c.last_computed_at.isoformat() if c.last_computed_at else None}


# ───────────────────────────────────────────────────────────────────────
# Funnels
# ───────────────────────────────────────────────────────────────────────


@router.post("/funnels")
async def create_funnel(
    body: FunnelIn,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    f = Funnel(
        workspace_id=wid, name=body.name, steps=body.steps,
        conversion_window_days=body.conversion_window_days,
    )
    db.add(f)
    await db.commit()
    return {"id": f.id}


@router.get("/funnels/{funnel_id}/results")
async def funnel_results(
    funnel_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    f = (await db.execute(
        select(Funnel).where(Funnel.id == funnel_id, Funnel.workspace_id == wid)
    )).scalar_one_or_none()
    if not f:
        raise HTTPException(404, "funnel-not-found")
    return await compute_funnel(db, f)


# ───────────────────────────────────────────────────────────────────────
# Embedded reports
# ───────────────────────────────────────────────────────────────────────


@router.post("/reports/embed/{dashboard_id}")
async def make_embed(
    dashboard_id: str,
    ttl_seconds: int = Query(86_400, ge=60, le=2_592_000),
    viewer_email: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    wid = await _ws(db, user_id)
    d = (await db.execute(
        select(Dashboard).where(
            Dashboard.id == dashboard_id, Dashboard.workspace_id == wid,
        )
    )).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "dashboard-not-found")
    try:
        token = sign_embed_token(
            workspace_id=wid, dashboard_id=d.id,
            viewer_email=viewer_email, ttl_seconds=ttl_seconds,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    base = os.getenv("HELEN_BASE_URL", "https://helen.local")
    audit_log("analytics.embed.created", user_id=user_id, success=True,
              details={"dashboard_id": d.id, "viewer": viewer_email})
    return {"token": token, "url": embed_url(
        base_url=base, dashboard_id=d.id, token=token,
    )}


@router.get("/embed/{dashboard_id}")
async def render_embed(
    dashboard_id: str,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    payload = verify_embed_token(token)
    if not payload or payload.get("dashboard_id") != dashboard_id:
        raise HTTPException(401, "invalid-or-expired-token")
    d = (await db.execute(
        select(Dashboard).where(Dashboard.id == dashboard_id)
    )).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "dashboard-not-found")
    return {
        "id": d.id, "name": d.name, "slug": d.slug,
        "layout": dict(d.layout or {}),
        "widgets": [
            {
                "id": w.id, "name": w.name, "kind": w.kind,
                "config": dict(w.config or {}),
                "position": dict(w.position or {}),
            } for w in (d.widgets or [])
        ],
        "viewer": payload.get("viewer"),
        "embedded": True,
    }
