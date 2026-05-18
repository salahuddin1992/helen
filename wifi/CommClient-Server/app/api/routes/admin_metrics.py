"""
Admin — Time-series metrics dashboard (Phase 2 / Module F).

Endpoints
---------
GET /api/admin/metrics/snapshot         — current values + boot time
GET /api/admin/metrics/series           — single-metric series, ?metric=, ?since=
GET /api/admin/metrics/all-series       — all metrics in one payload
GET /api/admin/metrics/prometheus       — Prometheus text format export
GET /api/admin/metrics/info             — capacity / resolution metadata
"""

from __future__ import annotations

import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.core.logging import get_logger
from app.core.security_utils import require_role
from app.services.metrics_collector import (
    HORIZON_SEC,
    METRIC_NAMES,
    RESOLUTION_SEC,
    MetricsCollector,
    metrics_collector,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/metrics", tags=["admin-phase2"])


# ── Helpers ───────────────────────────────────────────────

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)


def _parse_since(s: Optional[str]) -> float:
    """Accept '5m', '1h', '900' (seconds), default 1 hour."""
    if not s:
        return float(HORIZON_SEC)
    if s.isdigit():
        return float(s)
    m = _DURATION_RE.match(s)
    if not m:
        raise HTTPException(status_code=400, detail=f"Bad duration: {s}")
    n = int(m.group(1))
    unit = m.group(2).lower()
    return float(n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def _ensure_started() -> MetricsCollector:
    coll = metrics_collector
    coll.start()
    _install_default_probes(coll)
    return coll


_PROBES_INSTALLED = False


def _install_default_probes(coll: MetricsCollector) -> None:
    """Register lazy probes for application metrics. Each probe is wrapped
    in try/except inside the collector so a missing service silently
    yields the previous value."""
    global _PROBES_INSTALLED
    if _PROBES_INSTALLED:
        return
    _PROBES_INSTALLED = True

    def _active_calls() -> int:
        try:
            from app.services.active_call_service import active_call_service
            return len(getattr(active_call_service, "_calls", {}) or {})
        except Exception:
            try:
                from app.services import active_call_service as acs
                return len(getattr(acs, "_calls", {}) or {})
            except Exception:
                return 0

    def _connected_clients() -> int:
        try:
            from app.socketio_server import sio
            return len(getattr(sio, "manager", None).rooms.get("/", {}) or {})
        except Exception:
            return 0

    def _db_connections() -> int:
        try:
            from app.db.session import engine
            pool = engine.pool
            return int(pool.checkedout()) + int(pool.size())
        except Exception:
            return 0

    def _queue_depth() -> int:
        depth = 0
        try:
            from app.services.dlq_service import dlq_service
            depth += int(getattr(dlq_service, "depth", lambda: 0)() or 0)
        except Exception:
            pass
        try:
            from app.services.outbox import outbox_service
            depth += int(getattr(outbox_service, "pending_count", lambda: 0)() or 0)
        except Exception:
            pass
        return depth

    coll.register_probe("active_calls", _active_calls)
    coll.register_probe("connected_clients", _connected_clients)
    coll.register_probe("db_connections", _db_connections)
    coll.register_probe("queue_depth", _queue_depth)


# ── Endpoints ─────────────────────────────────────────────

@router.get("/info")
async def info(user_id: str = Depends(require_role("admin"))):
    _ensure_started()
    return {
        "resolution_sec": RESOLUTION_SEC,
        "horizon_sec": HORIZON_SEC,
        "metrics": list(METRIC_NAMES),
        "ts": time.time(),
    }


@router.get("/snapshot")
async def snapshot(user_id: str = Depends(require_role("admin"))):
    coll = _ensure_started()
    return coll.snapshot()


@router.get("/series")
async def series(
    user_id: str = Depends(require_role("admin")),
    metric: str = Query(..., description="one of the registered metric names"),
    since: Optional[str] = Query(None, description="e.g. '15m', '1h', '900'"),
):
    coll = _ensure_started()
    if metric not in METRIC_NAMES:
        raise HTTPException(status_code=400, detail=f"unknown metric {metric}")
    return coll.series(metric, since_sec=_parse_since(since))


@router.get("/all-series")
async def all_series(
    user_id: str = Depends(require_role("admin")),
    since: Optional[str] = Query(None),
):
    coll = _ensure_started()
    return coll.all_series(since_sec=_parse_since(since))


@router.get("/prometheus", response_class=PlainTextResponse)
async def prometheus(user_id: str = Depends(require_role("admin"))):
    """Plain-text Prometheus exposition format. Latest sample only."""
    coll = _ensure_started()
    snap = coll.snapshot()
    lines: list[str] = []
    for name, val in (snap.get("metrics") or {}).items():
        if val is None:
            continue
        safe = name.replace("-", "_")
        lines.append(f"# TYPE helen_{safe} gauge")
        lines.append(f"helen_{safe} {val}")
    lines.append("# TYPE helen_uptime_seconds gauge")
    lines.append(f"helen_uptime_seconds {snap.get('uptime_sec', 0)}")
    return "\n".join(lines) + "\n"
