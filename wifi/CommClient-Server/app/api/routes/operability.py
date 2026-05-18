"""
Operability admin endpoints — Group 3 modules.

Each module here is a *standalone* opt-in feature. The endpoints
return ``{"configured": false}`` when the module hasn't been
enabled by its env var, so the surface is always live (you can
hit it during a smoke test) without forcing the operator to
configure anything to make 200s appear.

Endpoints
---------
GET  /api/admin/ops/stun/status              self-hosted STUN responder stats
GET  /api/admin/ops/federation/discovery     mDNS-discovered federation candidates
POST /api/admin/ops/federation/discovery/drain
                                              atomically pop + return candidates
GET  /api/admin/ops/backup-verifier/status   verifier history + last result
POST /api/admin/ops/backup-verifier/run-now  trigger one verification immediately
GET  /api/admin/ops/federation/shaper/stats  per-peer bandwidth shaper stats
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.security_utils import require_role


router = APIRouter(prefix="/admin/ops", tags=["admin", "operability"])


# ── STUN responder ────────────────────────────────────────────────


@router.get("/stun/status")
async def stun_status(_: str = Depends(require_role("admin"))):
    from app.services.stun_responder import get_stun_responder
    s = get_stun_responder()
    if s is None:
        return {"configured": False}
    return {
        "configured": True,
        "bind_host": s.bind_host,
        "bind_port": s.bind_port,
        "stats": s.stats.to_dict(),
    }


# ── Federation autodiscovery ──────────────────────────────────────


@router.get("/federation/discovery")
async def federation_discovery_list(
    _: str = Depends(require_role("admin")),
):
    from app.services.federation_autodiscovery import list_candidates
    return {
        "candidates": [c.to_dict() for c in list_candidates()],
    }


@router.post("/federation/discovery/drain")
async def federation_discovery_drain(
    _: str = Depends(require_role("admin")),
):
    """Atomically read + clear the candidate ledger. Returns the
    list of candidates a federation bootstrap loop would have seen."""
    from app.services.federation_autodiscovery import drain_candidates
    return {
        "drained": [c.to_dict() for c in drain_candidates()],
    }


# ── Backup verifier ───────────────────────────────────────────────


@router.get("/backup-verifier/status")
async def backup_verifier_status(
    _: str = Depends(require_role("admin")),
):
    from app.services.backup_verifier import get_backup_verifier
    v = get_backup_verifier()
    if v is None:
        return {"configured": False}
    return {"configured": True, **v.status()}


@router.post("/backup-verifier/run-now")
async def backup_verifier_run_now(
    _: str = Depends(require_role("admin")),
):
    from app.services.backup_verifier import get_backup_verifier
    v = get_backup_verifier()
    if v is None:
        raise HTTPException(
            status_code=404, detail="backup verifier not configured",
        )
    r = await v.run_once()
    return r.to_dict()


# ── Federation shaper ─────────────────────────────────────────────


@router.get("/federation/shaper/stats")
async def shaper_stats(_: str = Depends(require_role("admin"))):
    from app.services.federation_shaper import get_federation_shaper
    s = get_federation_shaper()
    if s is None:
        return {"configured": False}
    return {
        "configured": True,
        "rate_bps": s.refill_rate,
        "capacity_bytes": s.capacity,
        "max_wait_s": s.max_wait_s,
        "peers": [st.to_dict() for st in s.all_stats() if st],
    }


__all__ = ["router"]
