"""
Chaos engineering admin endpoints — failure injection, congestion
simulation, route override, and trace inspection. Admin-only AND
gated by ``HELEN_ENABLE_100_HOP_TEST_MODE`` env flag.

Refusal semantics
-----------------
If the env flag is not set, every endpoint returns 403. We do NOT
silently no-op — the operator should know they tried to inject
failure into a server that won't comply.

Endpoints
---------
* ``POST /api/chaos/inject_failure``        — set per-target failure rate
* ``POST /api/chaos/inject_congestion``     — push fake load to make a server appear overloaded
* ``POST /api/chaos/force_route``           — pin a specific route for the next event with given trace_id
* ``DELETE /api/chaos/inject_failure/{id}`` — clear failure injection for a target
* ``GET  /api/chaos/state``                 — current injections
* ``GET  /api/chaos/traces``                — recent traces (paginated)
* ``GET  /api/chaos/traces/{trace_id}``     — full trace with hops
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.user import User
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(prefix="/chaos", tags=["chaos"])


def _chaos_enabled() -> bool:
    raw = os.environ.get("HELEN_ENABLE_100_HOP_TEST_MODE", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


async def _require_chaos_admin(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> str:
    if not _chaos_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "chaos mode disabled — set HELEN_ENABLE_100_HOP_TEST_MODE=true "
                "to enable. Refused for safety."
            ),
        )
    user = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if user is None or getattr(user, "role", None) != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required for chaos endpoints",
        )
    return user_id


# ── Chaos state — process-local with optional Redis mirror ─────────
# Hit-each-server endpoint pattern works for ≤10 servers but doesn't
# scale to 100. When ``HELEN_REDIS_URL`` is configured we MIRROR
# every mutation to a Redis hash so injection on Server A becomes
# visible on Servers B, C, …. The local dict stays as the read fast
# path (no cross-server roundtrip on the chaos hook hot path).
#
# Keys:
#   helen:chaos:failures        HASH  target -> failure_rate (str float)
#   helen:chaos:congestion      HASH  server_id -> JSON load overrides
#   helen:chaos:routes          HASH  trace_id -> JSON route list
# All three have a 1h TTL so a forgotten injection eventually fades.

_failure_injections: dict[str, float] = {}  # target → failure_rate (0–1)
_congestion_injections: dict[str, dict] = {}  # server_id → fake load metrics
_route_overrides: dict[str, list[str]] = {}  # trace_id → forced route

_CHAOS_KEY_FAIL = "helen:chaos:failures"
_CHAOS_KEY_CONG = "helen:chaos:congestion"
_CHAOS_KEY_ROUTES = "helen:chaos:routes"
_CHAOS_TTL_SEC = 3600


def _redis_client():
    """Best-effort. Returns the configured client, or None when no
    Redis is wired (single-server LAN). All callers must tolerate
    None and fall through to local-only state."""
    try:
        # The presence service holds the same client we passed in
        # at startup. Cheap singleton lookup.
        from app.services.distributed_presence_service import get_presence_service
        svc = get_presence_service()
        return getattr(svc, "_redis", None)
    except Exception:
        return None


async def _mirror_failure_set(target: str, rate: float) -> None:
    r = _redis_client()
    if r is None:
        return
    try:
        async with r.pipeline(transaction=False) as p:
            p.hset(_CHAOS_KEY_FAIL, target, str(rate))
            p.expire(_CHAOS_KEY_FAIL, _CHAOS_TTL_SEC)
            await p.execute()
    except Exception as e:
        logger.warning("chaos_mirror_fail_set_failed", error=str(e))


async def _mirror_failure_clear(target: str) -> None:
    r = _redis_client()
    if r is None:
        return
    try:
        await r.hdel(_CHAOS_KEY_FAIL, target)
    except Exception as e:
        logger.warning("chaos_mirror_fail_clear_failed", error=str(e))


async def _mirror_congestion_set(server_id: str, fake: dict) -> None:
    r = _redis_client()
    if r is None:
        return
    import json as _json
    try:
        async with r.pipeline(transaction=False) as p:
            p.hset(_CHAOS_KEY_CONG, server_id, _json.dumps(fake))
            p.expire(_CHAOS_KEY_CONG, _CHAOS_TTL_SEC)
            await p.execute()
    except Exception as e:
        logger.warning("chaos_mirror_cong_set_failed", error=str(e))


async def _mirror_congestion_clear(server_id: str) -> None:
    r = _redis_client()
    if r is None:
        return
    try:
        await r.hdel(_CHAOS_KEY_CONG, server_id)
    except Exception as e:
        logger.warning("chaos_mirror_cong_clear_failed", error=str(e))


async def _mirror_route_set(trace_id: str, route: list[str]) -> None:
    r = _redis_client()
    if r is None:
        return
    import json as _json
    try:
        async with r.pipeline(transaction=False) as p:
            p.hset(_CHAOS_KEY_ROUTES, trace_id, _json.dumps(route))
            p.expire(_CHAOS_KEY_ROUTES, _CHAOS_TTL_SEC)
            await p.execute()
    except Exception as e:
        logger.warning("chaos_mirror_route_set_failed", error=str(e))


async def _mirror_route_consume(trace_id: str) -> Optional[list[str]]:
    """Used on the hot read-path — returns + deletes the override
    atomically so two servers can't both consume the same forced
    route. Best-effort; returns None if Redis is missing or the key
    isn't there."""
    r = _redis_client()
    if r is None:
        return None
    import json as _json
    try:
        # GETDEL-style: lua script to read+del atomically. HGET +
        # HDEL pipeline isn't atomic across the network but it's
        # close enough for chaos lab use.
        async with r.pipeline(transaction=True) as p:
            p.hget(_CHAOS_KEY_ROUTES, trace_id)
            p.hdel(_CHAOS_KEY_ROUTES, trace_id)
            res = await p.execute()
        raw = res[0]
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return _json.loads(raw)
    except Exception as e:
        logger.warning("chaos_mirror_route_consume_failed", error=str(e))
        return None


# ── Schemas ────────────────────────────────────────────────────────


class InjectFailureRequest(BaseModel):
    target: str = Field(
        ...,
        description="target identifier (e.g. 'peer:server_037' or 'subject:fabric.P0.*')",
    )
    failure_rate: float = Field(
        ..., ge=0.0, le=1.0,
        description="probability that calls to target will be made to fail (0–1)",
    )
    note: Optional[str] = None


class InjectCongestionRequest(BaseModel):
    server_id: str
    cpu_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    memory_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    queue_depth_p0: Optional[int] = Field(default=None, ge=0)
    queue_depth_p1: Optional[int] = Field(default=None, ge=0)
    health_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class ForceRouteRequest(BaseModel):
    trace_id: str
    route: list[str] = Field(..., min_length=2)


# ── Public read API ───────────────────────────────────────────────


def get_failure_rate(target: str) -> float:
    """Used by circuit_breaker / route_executor / broker_client to
    consult the current failure-injection rate for a target.

    Sync hot path — Redis lookup would block the loop. We rely on the
    mirror sync that happens on each mutation; locally cached value
    is good enough for chaos hot paths (fail-on-rate is statistical).
    """
    return _failure_injections.get(target, 0.0)


def get_fake_load(server_id: str) -> Optional[dict]:
    return _congestion_injections.get(server_id)


def get_forced_route(trace_id: str) -> Optional[list[str]]:
    """Sync wrapper used by route_planner. Local consume only — for
    cross-server forced routes the caller should use the async
    `consume_forced_route` variant inside an async handler."""
    return _route_overrides.pop(trace_id, None)


async def consume_forced_route(trace_id: str) -> Optional[list[str]]:
    """Async variant that also consults the Redis mirror so a
    forced route registered on Server A actually fires on Server B
    (the executor that picks up the trace). Local lookup first to
    avoid an unnecessary Redis round-trip on the common no-override
    path."""
    local = _route_overrides.pop(trace_id, None)
    if local is not None:
        return local
    return await _mirror_route_consume(trace_id)


async def refresh_local_from_redis() -> None:
    """Periodic refresh — repopulates local dicts from the Redis
    mirror so a brand-new server can see existing chaos state on
    boot. Safe to call repeatedly. Best-effort."""
    r = _redis_client()
    if r is None:
        return
    import json as _json
    try:
        # Failure injections.
        raw = await r.hgetall(_CHAOS_KEY_FAIL)
        if raw:
            _failure_injections.clear()
            for k, v in raw.items():
                k = k.decode("utf-8") if isinstance(k, bytes) else k
                v = v.decode("utf-8") if isinstance(v, bytes) else v
                try:
                    _failure_injections[k] = float(v)
                except ValueError:
                    pass
        # Congestion injections.
        raw = await r.hgetall(_CHAOS_KEY_CONG)
        if raw:
            _congestion_injections.clear()
            for k, v in raw.items():
                k = k.decode("utf-8") if isinstance(k, bytes) else k
                v = v.decode("utf-8") if isinstance(v, bytes) else v
                try:
                    _congestion_injections[k] = _json.loads(v)
                except Exception:
                    pass
        # Forced routes — kept in Redis only (they're consume-on-read
        # and we don't want every server's local dict to mirror them).
    except Exception as e:
        logger.warning("chaos_refresh_local_failed", error=str(e))


# ── Endpoints ──────────────────────────────────────────────────────


@router.post("/inject_failure")
async def inject_failure(
    req: InjectFailureRequest,
    user_id: str = Depends(_require_chaos_admin),
):
    _failure_injections[req.target] = req.failure_rate
    await _mirror_failure_set(req.target, req.failure_rate)
    logger.warning(
        "chaos_inject_failure",
        target=req.target,
        failure_rate=req.failure_rate,
        by=user_id,
        note=req.note,
    )
    return {"ok": True, "target": req.target, "failure_rate": req.failure_rate}


@router.delete("/inject_failure/{target}")
async def clear_failure(
    target: str,
    user_id: str = Depends(_require_chaos_admin),
):
    removed = _failure_injections.pop(target, None)
    await _mirror_failure_clear(target)
    return {"ok": True, "target": target, "previous_rate": removed}


@router.post("/inject_congestion")
async def inject_congestion(
    req: InjectCongestionRequest,
    user_id: str = Depends(_require_chaos_admin),
):
    fake = {k: v for k, v in req.model_dump().items() if v is not None and k != "server_id"}
    if not fake:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="at least one metric override required",
        )
    _congestion_injections[req.server_id] = fake
    await _mirror_congestion_set(req.server_id, fake)
    logger.warning(
        "chaos_inject_congestion",
        server_id=req.server_id,
        overrides=fake,
        by=user_id,
    )
    return {"ok": True, "server_id": req.server_id, "overrides": fake}


@router.delete("/inject_congestion/{server_id}")
async def clear_congestion(
    server_id: str,
    user_id: str = Depends(_require_chaos_admin),
):
    removed = _congestion_injections.pop(server_id, None)
    await _mirror_congestion_clear(server_id)
    return {"ok": True, "server_id": server_id, "previous_overrides": removed}


@router.post("/force_route")
async def force_route(
    req: ForceRouteRequest,
    user_id: str = Depends(_require_chaos_admin),
):
    _route_overrides[req.trace_id] = list(req.route)
    await _mirror_route_set(req.trace_id, list(req.route))
    logger.warning(
        "chaos_force_route",
        trace_id=req.trace_id,
        route=req.route,
        by=user_id,
    )
    return {"ok": True, "trace_id": req.trace_id, "route_length": len(req.route)}


@router.get("/state")
async def chaos_state(user_id: str = Depends(_require_chaos_admin)):
    return {
        "enabled": True,
        "failure_injections": _failure_injections,
        "congestion_injections": _congestion_injections,
        "route_overrides_pending": list(_route_overrides.keys()),
    }


@router.get("/traces")
async def list_traces(
    user_id: str = Depends(_require_chaos_admin),
    limit: int = Query(default=50, ge=1, le=500),
    outcome: Optional[str] = Query(default=None),
):
    from app.services.trace_collector_service import trace_collector
    traces = await trace_collector.list_recent_traces(limit=limit, outcome=outcome)
    return {"traces": traces, "count": len(traces)}


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    user_id: str = Depends(_require_chaos_admin),
):
    from app.services.trace_collector_service import trace_collector
    trace = await trace_collector.get_trace(trace_id)
    if trace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="trace not found",
        )
    return trace
