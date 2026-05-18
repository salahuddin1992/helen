"""
Admin Monitoring Dashboard API — Helen Server.

Mounted under ``/api/admin``. All routes require an admin-level bearer token.

Endpoints
---------
- ``GET  /observability/metrics``         — host + service metrics + alerts
- ``GET  /transports/{name}/status``      — per-transport probe snapshot
- ``GET  /connections/list``              — paginated active connections
- ``POST /connections/{conn_id}/kick``    — disconnect a connection (audited)
- ``POST /clients/{client_id}/disconnect``— legacy alias for kick
- ``WS   /ws/metrics``                    — 1Hz stream of metrics + alerts

The monitoring router is fully self-contained: it does not require the
``MetricsCollector`` background task to be running (it will lazy-start it
on first request to ``/observability/metrics``), but production deployments
should start it from the FastAPI lifespan handler for accurate windowed data.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    import structlog
    logger = structlog.get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)


# ── Auth dependency — prefer project's existing RBAC, fall back gracefully ──

require_admin: Any  # late-bound below

try:
    # Preferred: existing project dependency (app/api/deps.py)
    from app.api.deps import require_admin as _require_admin  # type: ignore
    require_admin = _require_admin
except Exception:
    try:
        # Helen Server pattern: factory dependency in app/core/security_utils.py
        from app.core.security_utils import require_role  # type: ignore
        require_admin = require_role("admin")
    except Exception:  # pragma: no cover — last-resort fallback
        _bearer = HTTPBearer()

        async def _fallback_require_admin(
            creds: HTTPAuthorizationCredentials = Depends(_bearer),
        ) -> str:
            try:
                from app.core.security import decode_token  # type: ignore
                payload = decode_token(creds.credentials)
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid authentication token",
                )
            role = payload.get("role", "user")
            if role not in ("admin", "superadmin", "root"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admin privileges required",
                )
            uid = payload.get("sub")
            if not uid:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token missing subject claim",
                )
            return str(uid)

        require_admin = _fallback_require_admin


# ── Audit hook — soft-import so missing service doesn't break the router ──

def _audit_destructive(
    actor: str,
    action: str,
    target: str,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """
    Append a destructive admin action to the tamper-evident audit chain.
    Silently falls back to structlog if the chain service is unavailable.
    """
    try:
        from app.services.audit_chain import get_audit_chain  # type: ignore
        chain = get_audit_chain()
        if chain is not None:
            chain.append(
                actor=actor,
                action=action,
                target=target,
                payload=extra or {},
            )
            return
    except Exception:
        pass

    try:
        from app.core.audit import audit_log  # type: ignore
        audit_log(
            event=action, user_id=actor, success=True,
            details={"target": target, **(extra or {})},
        )
        return
    except Exception:
        pass

    # TODO: integrate app.services.audit.chain.append_entry once the unified
    #       audit service lands. Until then, fall back to structlog.
    logger.warning("audit_chain_missing",
                   actor=actor, action=action, target=target, extra=extra)


# ── Service imports ────────────────────────────────────────────────────────

from app.services.monitoring.metrics_collector import (
    SUPPORTED_TRANSPORTS,
    get_metrics_collector,
)
from app.services.monitoring.connection_registry import get_connection_registry
from app.services.monitoring.ws_streamer import get_ws_manager, verify_admin_token


router = APIRouter(prefix="/api/admin", tags=["admin-monitoring"])


# ── /observability/metrics ─────────────────────────────────────────────────

@router.get("/observability/metrics")
async def get_observability_metrics(
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    """
    Return host + service metrics + recent alerts.
    Shape:
        {
          ts, cpu, mem, net_in_mbps, net_out_mbps, disk_io_mbps,
          rps, errors, rtt_ms,
          alerts: [{severity, message, timestamp}, ...]
        }
    """
    collector = get_metrics_collector()
    # Lazy-start the collector if running inside an event loop and not yet up.
    if collector._task is None or collector._task.done():
        try:
            await collector.start_collector()
        except Exception as exc:
            logger.warning("collector_lazy_start_failed", error=str(exc))
    try:
        return await collector.collect_current()
    except Exception as exc:
        logger.error("metrics_collect_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to collect metrics",
        )


# ── /transports/{name}/status ──────────────────────────────────────────────

@router.get("/transports/{name}/status")
async def get_transport_status(
    name: str,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    """Per-transport probe snapshot. Cached for 2s to throttle storms."""
    name_norm = name.lower().strip()
    if name_norm not in SUPPORTED_TRANSPORTS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown transport '{name}'. "
                   f"Supported: {sorted(SUPPORTED_TRANSPORTS)}",
        )
    try:
        snap = await get_metrics_collector().transport_status(name_norm)
        return snap.to_dict()
    except Exception as exc:
        logger.error("transport_status_failed", transport=name_norm, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query transport",
        )


# ── /connections/list ──────────────────────────────────────────────────────

@router.get("/connections/list")
async def list_connections(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    transport: Optional[str] = Query(None, max_length=64),
    search: Optional[str] = Query(None, max_length=128),
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    """Paginated active connections; ordered by newest first."""
    try:
        rows, total = await get_connection_registry().list(
            limit=limit, offset=offset, transport=transport, search=search,
        )
    except Exception as exc:
        logger.error("connection_list_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enumerate connections",
        )
    return {
        "items": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ── /connections/{conn_id}/kick ────────────────────────────────────────────

@router.post("/connections/{conn_id}/kick")
async def kick_connection(
    conn_id: str,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    """Forcefully terminate a live connection. Logged to the audit chain."""
    registry = get_connection_registry()
    ok = await registry.kick(conn_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connection '{conn_id}' not found",
        )
    _audit_destructive(
        actor=user_id, action="admin.connection_kicked",
        target=conn_id, extra={"reason": "admin_action"},
    )
    return {"ok": True, "conn_id": conn_id, "action": "kicked"}


# ── /clients/{client_id}/disconnect (legacy alias) ─────────────────────────

@router.post("/clients/{client_id}/disconnect")
async def disconnect_client(
    client_id: str,
    user_id: str = Depends(require_admin),
) -> dict[str, Any]:
    """Backwards-compatible alias for ``/connections/{id}/kick``."""
    registry = get_connection_registry()
    ok = await registry.kick(client_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Client '{client_id}' not found",
        )
    _audit_destructive(
        actor=user_id, action="admin.client_disconnected",
        target=client_id, extra={"reason": "legacy_endpoint"},
    )
    return {"ok": True, "client_id": client_id, "action": "disconnected"}


# ── /ws/metrics ────────────────────────────────────────────────────────────

@router.websocket("/ws/metrics")
async def ws_metrics(websocket: WebSocket, token: Optional[str] = Query(None)) -> None:
    """
    Real-time metrics feed.

    Auth: ``?token=<jwt>`` or ``Authorization: Bearer <jwt>`` header.
    Frames:
        {"type":"metric", "metrics":{...}}     — every 1s
        {"type":"alert",  "alert":{...}}       — on alert generation
    """
    # Prefer query token; fall back to Authorization header
    if not token:
        auth = websocket.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()

    if not token or verify_admin_token(token) is None:
        await websocket.close(code=4401)
        return

    manager = get_ws_manager()
    ok = await manager.connect(websocket, token)
    if not ok:
        return

    # Ensure the metrics collector is running so frames have real data
    collector = get_metrics_collector()
    if collector._task is None or collector._task.done():
        try:
            await collector.start_collector()
        except Exception:
            pass

    try:
        # Keep socket alive; clients may send pings but we ignore payloads.
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    except Exception as exc:
        logger.warning("ws_metrics_error", error=str(exc))
    finally:
        await manager.disconnect(websocket)


@router.get("/health", include_in_schema=False)
async def admin_health():
    """Lightweight health probe for the admin namespace — no auth required."""
    import time
    return {"status": "ok", "ts": int(time.time()), "service": "helen-admin"}
