"""
Edge admin REST endpoints. Requires ``edge.admin``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.edge import EdgeNode, EdgeRegion, EdgeRoute, RegionPolicy
from app.services.edge.edge_sync import list_channels
from app.services.edge.latency_steering import get_latency_steering
from app.services.rbac.enforcer import require_permission

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin/edge", tags=["admin-edge"])
_PERM = "edge.admin"


class EdgeNodeIn(BaseModel):
    node_id: str
    region: str
    city: str = ""
    country: str = ""
    datacenter: str = ""
    advertise_url: str
    public_url: str = ""
    geo_lat: float = 0.0
    geo_lng: float = 0.0
    capacity: dict[str, Any] = {}


class EdgeNodeOut(BaseModel):
    id: str
    node_id: str
    region: str
    city: str
    country: str
    datacenter: str
    advertise_url: str
    public_url: str
    geo_lat: float
    geo_lng: float
    status: str
    current_load_percent: float
    last_heartbeat: datetime


class RegionIn(BaseModel):
    code: str
    name: str
    country: str = ""
    data_residency_required: bool = False
    gdpr_compliant: bool = False
    latency_zone: str = "warm"


class ResidencyIn(BaseModel):
    allowed_regions: list[str] = []
    required_residency_region: Optional[str] = None
    encryption_at_rest_required: bool = False
    audit_log_required: bool = True


@router.get("/nodes", response_model=list[EdgeNodeOut])
async def list_nodes(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(
        select(EdgeNode).order_by(EdgeNode.region, EdgeNode.node_id)
    )).scalars().all()
    return [
        EdgeNodeOut(
            id=r.id, node_id=r.node_id, region=r.region, city=r.city,
            country=r.country, datacenter=r.datacenter,
            advertise_url=r.advertise_url, public_url=r.public_url,
            geo_lat=r.geo_lat, geo_lng=r.geo_lng, status=r.status,
            current_load_percent=r.current_load_percent,
            last_heartbeat=r.last_heartbeat,
        )
        for r in rows
    ]


@router.post("/nodes")
async def register_node(
    payload: EdgeNodeIn,
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    existing = (await db.execute(
        select(EdgeNode).where(EdgeNode.node_id == payload.node_id)
    )).scalar_one_or_none()
    if existing is not None:
        for k, v in payload.dict().items():
            setattr(existing, k, v)
        row = existing
    else:
        row = EdgeNode(**payload.dict())
        db.add(row)
    await db.commit()
    return {"ok": True, "id": row.id, "node_id": row.node_id}


@router.delete("/nodes/{id}")
async def remove_node(
    id: str = Path(...),
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(EdgeNode).where(EdgeNode.id == id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.get("/regions")
async def list_regions(
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    rows = (await db.execute(select(EdgeRegion).order_by(EdgeRegion.code))).scalars().all()
    return [
        {
            "id":                       r.id,
            "code":                     r.code,
            "name":                     r.name,
            "country":                  r.country,
            "data_residency_required":  r.data_residency_required,
            "gdpr_compliant":           r.gdpr_compliant,
            "latency_zone":             r.latency_zone,
        }
        for r in rows
    ]


@router.post("/regions")
async def create_region(
    payload: RegionIn,
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    existing = (await db.execute(
        select(EdgeRegion).where(EdgeRegion.code == payload.code)
    )).scalar_one_or_none()
    if existing:
        for k, v in payload.dict().items():
            setattr(existing, k, v)
    else:
        existing = EdgeRegion(**payload.dict())
        db.add(existing)
    await db.commit()
    return {"ok": True, "id": existing.id}


@router.get("/routing-decisions")
async def routing_decisions(
    _u: str = Depends(require_permission(_PERM)),
):
    return {"channels": list_channels()}


@router.get("/latency-matrix")
async def latency_matrix(
    _u: str = Depends(require_permission(_PERM)),
):
    return {"matrix": get_latency_steering().matrix()}


@router.post("/workspaces/{workspace_id}/residency")
async def set_residency(
    workspace_id: str,
    payload: ResidencyIn,
    db: AsyncSession = Depends(get_db),
    _u: str = Depends(require_permission(_PERM)),
):
    row = (await db.execute(
        select(RegionPolicy).where(RegionPolicy.workspace_id == workspace_id)
    )).scalar_one_or_none()
    if row is None:
        row = RegionPolicy(workspace_id=workspace_id, **payload.dict())
        db.add(row)
    else:
        for k, v in payload.dict().items():
            setattr(row, k, v)
    await db.commit()
    return {"ok": True, "workspace_id": workspace_id}
