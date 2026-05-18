"""Public Service Discovery API.

Mounted at ``/api/discovery/*``.

Public endpoints (signature-required for write paths):

  POST /api/discovery/register          — register/update a service
  POST /api/discovery/heartbeat         — refresh ttl
  POST /api/discovery/deregister        — remove a service
  GET  /api/discovery/services          — list all services
  GET  /api/discovery/services/{type}   — list by type
  POST /api/discovery/find              — find best for criteria
  POST /api/discovery/federation/find   — answer cross-cluster lookup

All write paths verify HMAC-signed records. Read paths are
authenticated only by network reachability (LAN-first model).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/discovery", tags=["service-discovery"])


# ── Schemas ────────────────────────────────────────────────────


class RegisterBody(BaseModel):
    """A full ServiceRecord JSON payload (with signature)."""
    service_id: str
    service_type: str
    server_id: str = ""
    host: str
    port: int
    protocol: str = "http"
    public_url: str = ""
    cluster_id: str = "default"
    region: str = "default"
    zone: str = "default"
    ttl_sec: float = 60.0
    max_capacity: int = 0
    current_load: int = 0
    advertised_latency_ms: float = 0.0
    capabilities: dict = {}
    tags: list[str] = []
    signature: str
    signed_at: float
    pubkey_fingerprint: str = ""


class HeartbeatBody(BaseModel):
    service_id: str
    current_load: Optional[int] = None
    status: Optional[str] = None


class FindBody(BaseModel):
    service_type: str
    region: Optional[str] = None
    zone: Optional[str] = None
    cluster_id: Optional[str] = None
    required_caps: Optional[dict] = None
    required_tags: Optional[list[str]] = None
    k: int = 1


class FederationFindBody(BaseModel):
    service_type: str
    region: Optional[str] = None
    k: int = 3


# ── Write endpoints ────────────────────────────────────────────


@router.post("/register")
async def register(body: RegisterBody):
    from app.service_discovery.service_record import (
        ServiceRecord, ServiceType,
    )
    from app.service_discovery.service_registry import get_registry
    from app.service_discovery.discovery_exceptions import (
        ServiceRegistrationError,
    )
    try:
        rec = ServiceRecord.from_dict(body.model_dump())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid_record:{e}")
    try:
        rec = get_registry().register(rec, verify_signature=True)
    except ServiceRegistrationError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ok": True, "record": rec.to_dict()}


@router.post("/heartbeat")
async def heartbeat(body: HeartbeatBody):
    from app.service_discovery.service_record import ServiceStatus
    from app.service_discovery.service_registry import get_registry
    from app.service_discovery.discovery_exceptions import ServiceNotFoundError
    status = None
    if body.status:
        try:
            status = ServiceStatus(body.status)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad_status")
    try:
        rec = get_registry().heartbeat(
            body.service_id,
            current_load=body.current_load,
            status=status,
        )
    except ServiceNotFoundError:
        raise HTTPException(status_code=404, detail="service_not_found")
    return {"ok": True, "record": rec.to_dict()}


@router.post("/deregister")
async def deregister(body: HeartbeatBody):
    from app.service_discovery.service_registry import get_registry
    ok = get_registry().deregister(body.service_id)
    return {"ok": ok}


# ── Read endpoints ─────────────────────────────────────────────


@router.get("/services")
async def list_services(
    type: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
):
    from app.service_discovery.service_record import ServiceType
    from app.service_discovery.service_registry import get_registry
    reg = get_registry()
    if type:
        try:
            st = ServiceType(type)
        except ValueError:
            raise HTTPException(status_code=400, detail="unknown_type")
        records = reg.by_type(st)
    elif region:
        records = reg.by_region(region)
    else:
        records = reg.all()
    return {"services": [r.to_dict() for r in records]}


@router.post("/find")
async def find_best_endpoint(body: FindBody):
    from app.service_discovery.service_record import ServiceType
    from app.service_discovery.service_lookup import find_top_k
    from app.service_discovery.discovery_exceptions import ServiceNotFoundError
    try:
        st = ServiceType(body.service_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="unknown_type")
    try:
        top = find_top_k(
            st,
            k=max(1, int(body.k)),
            region=body.region, zone=body.zone,
            cluster_id=body.cluster_id,
            required_caps=body.required_caps,
            required_tags=set(body.required_tags or []),
        )
    except ServiceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "results": [
            {
                "record":    r.to_dict(),
                "score":     score_val,
                "breakdown": breakdown,
            }
            for r, score_val, breakdown in top
        ],
    }


@router.post("/federation/find")
async def federation_find(body: FederationFindBody):
    from app.service_discovery.federation_lookup import (
        serve_federation_request,
    )
    return serve_federation_request(body.model_dump())


@router.get("/health")
async def health_summary():
    from app.service_discovery import get_discovery_manager
    return get_discovery_manager().snapshot()
