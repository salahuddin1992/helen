"""
Prometheus exposition endpoint for the distributed-fabric services.

Returns metrics in Prometheus text format from every Tier S/A/B
service that tracks counters: route_executor, event_ack_manager,
broker_client, event_priority_queue, load_monitor, fabric_subscribers,
trace_collector, federation_service circuit breakers.

Endpoint
--------
::

    GET /api/metrics

Authentication
--------------
Two modes (whichever applies first):

1. ``HELEN_METRICS_TOKEN`` env var set → ``Authorization: Bearer
   <token>`` required. Standard pattern for Prometheus scrape jobs.
2. ``HELEN_METRICS_TOKEN`` unset → admin role required (uses the
   existing JWT auth path). Default for ad-hoc inspection.

A misconfigured deployment that wants public metrics can set
``HELEN_METRICS_PUBLIC=1`` — refused unless ``HELEN_ENV != production``
to prevent accidental exposure.

Format
------
Standard Prometheus text exposition:

::

    # HELP helen_envelope_published_total Number of envelopes published.
    # TYPE helen_envelope_published_total counter
    helen_envelope_published_total{source="route_executor"} 12345
    helen_envelope_published_total{source="broker"} 12350
    ...

Histograms / quantiles are not implemented yet — counters and gauges
only. A scrape job + Prometheus rules can compute rate() and slo()
from the counters at query time.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.user import User

logger = get_logger(__name__)
router = APIRouter(prefix="/metrics", tags=["metrics"])


def _is_public_metrics_allowed() -> bool:
    if os.environ.get("HELEN_METRICS_PUBLIC", "").lower() not in {"1", "true", "yes"}:
        return False
    # Production deployments: refuse public metrics regardless of flag.
    return os.environ.get("HELEN_ENV", "").lower() not in {"production", "prod"}


async def _auth_metrics(
    authorization: Optional[str] = Header(default=None),
    db: Optional[AsyncSession] = Depends(get_db),
) -> str:
    """Return user_id (or "scrape") on success, raise 401/403 on
    failure. Two paths handled here so we don't burden every metric
    line with auth scaffolding."""
    if _is_public_metrics_allowed():
        return "public"

    expected_token = os.environ.get("HELEN_METRICS_TOKEN", "").strip()
    if expected_token:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="bearer token required")
        token = authorization.removeprefix("Bearer ").strip()
        if token != expected_token:
            raise HTTPException(status_code=401, detail="invalid metrics token")
        return "scrape"

    # Fall back to JWT admin gate.
    try:
        from app.core.deps import get_current_user_id_from_header
        # Re-use the existing auth dep manually since FastAPI's Depends
        # composition gets messy with optional headers. Pull user_id
        # from JWT in Authorization.
        user_id = await get_current_user_id_from_header(authorization)
    except Exception:
        # Not all installs export get_current_user_id_from_header. Try
        # the regular dep one more time via a synthetic call.
        if not authorization:
            raise HTTPException(status_code=401, detail="auth required")
        raise HTTPException(status_code=401, detail="metrics auth failed")

    if db is not None and user_id:
        user = (await db.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()
        if user is None or getattr(user, "role", None) != "admin":
            raise HTTPException(status_code=403, detail="admin role required")
    return user_id or "unknown"


# ── Prometheus formatting helpers ──────────────────────────────────


def _esc_label(s: str) -> str:
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _line(metric: str, value: float | int, labels: Optional[dict] = None) -> str:
    if labels:
        kv = ",".join(f'{k}="{_esc_label(v)}"' for k, v in labels.items())
        return f"{metric}{{{kv}}} {value}"
    return f"{metric} {value}"


def _help(metric: str, description: str, mtype: str = "counter") -> str:
    return f"# HELP {metric} {description}\n# TYPE {metric} {mtype}"


# ── Sources of truth for each metric block ─────────────────────────


def _route_executor_metrics() -> list[str]:
    out = []
    try:
        from app.services.route_executor import get_executor
        ex = get_executor()
        if ex is None:
            return out
        m = ex.metrics()
        out.append(_help("helen_route_executor_events_total",
                         "Total envelopes processed by route_executor by outcome.", "counter"))
        for k in ("executed", "delivered_local", "forwarded", "loop_blocked",
                  "expired", "max_hops", "destination_unknown", "publish_failed"):
            out.append(_line(
                "helen_route_executor_events_total",
                m.get(k, 0),
                labels={"outcome": k},
            ))
    except Exception as e:
        logger.debug("metrics_route_executor_unavailable", error=str(e))
    return out


def _ack_manager_metrics() -> list[str]:
    out = []
    try:
        from app.services.event_ack_manager import get_ack_manager
        m = get_ack_manager().metrics()
        out.append(_help("helen_ack_events_total",
                         "ACK manager event counters.", "counter"))
        for k in ("tracked", "acked", "retried", "dlq_after_retries", "expired_no_ack"):
            out.append(_line("helen_ack_events_total",
                             m.get(k, 0), labels={"outcome": k}))
        out.append(_help("helen_ack_in_flight", "ACK envelopes currently in flight.", "gauge"))
        out.append(_line("helen_ack_in_flight", m.get("in_flight", 0)))
    except Exception as e:
        logger.debug("metrics_ack_unavailable", error=str(e))
    return out


def _broker_metrics() -> list[str]:
    out = []
    try:
        from app.services.broker_client import get_broker
        b = get_broker()
        if b is None:
            return out
        m = b.metrics()
        out.append(_help("helen_broker_events_total",
                         "Broker client events by direction/outcome.", "counter"))
        for k in ("published", "consumed", "publish_failed", "consume_failed"):
            out.append(_line("helen_broker_events_total",
                             m.get(k, 0), labels={"outcome": k}))
    except Exception as e:
        logger.debug("metrics_broker_unavailable", error=str(e))
    return out


def _priority_queue_metrics() -> list[str]:
    out = []
    try:
        from app.services.event_priority_queue import get_router
        m = get_router().metrics()
        out.append(_help("helen_priority_queue_depth",
                         "Current depth per priority queue.", "gauge"))
        out.append(_help("helen_priority_queue_events_total",
                         "Per-priority queue counters.", "counter"))
        for prio, stats in m.items():
            out.append(_line("helen_priority_queue_depth",
                             stats.get("depth", 0), labels={"priority": prio}))
            for k in ("published", "consumed", "dropped_overflow",
                      "dropped_expired", "dropped_dlq"):
                out.append(_line("helen_priority_queue_events_total",
                                 stats.get(k, 0),
                                 labels={"priority": prio, "outcome": k}))
    except Exception as e:
        logger.debug("metrics_pq_unavailable", error=str(e))
    return out


def _load_monitor_metrics() -> list[str]:
    out = []
    try:
        from app.services.load_monitor import get_load_monitor
        lm = get_load_monitor()
        if lm is None or lm.last is None:
            return out
        snap = lm.last
        out.append(_help("helen_load_cpu_percent", "Last CPU sample.", "gauge"))
        out.append(_line("helen_load_cpu_percent", snap.cpu_percent))
        out.append(_help("helen_load_memory_percent", "Last memory sample.", "gauge"))
        out.append(_line("helen_load_memory_percent", snap.memory_percent))
        out.append(_help("helen_load_event_loop_lag_ms",
                         "Asyncio event loop lag in milliseconds.", "gauge"))
        out.append(_line("helen_load_event_loop_lag_ms", snap.event_loop_lag_ms))
        out.append(_help("helen_load_active_sockets",
                         "Currently connected Socket.IO clients.", "gauge"))
        out.append(_line("helen_load_active_sockets", snap.active_sockets))
        out.append(_help("helen_load_active_calls",
                         "Currently active calls on this server.", "gauge"))
        out.append(_line("helen_load_active_calls", snap.active_calls))
        out.append(_help("helen_load_health_score",
                         "Derived health 0..1 (1=healthy).", "gauge"))
        out.append(_line("helen_load_health_score", snap.health_score))
    except Exception as e:
        logger.debug("metrics_load_unavailable", error=str(e))
    return out


def _fabric_subscribers_metrics() -> list[str]:
    out = []
    try:
        from app.socket.server_fabric_handlers import get_fabric_subscribers
        sub = get_fabric_subscribers()
        if sub is None:
            return out
        m = sub.metrics()
        out.append(_help("helen_fabric_subscriber_events_total",
                         "Fabric subscriber counters.", "counter"))
        for k in ("received_total", "delivered_local", "ack_received",
                  "deliver_failed", "consumer_restarts"):
            out.append(_line("helen_fabric_subscriber_events_total",
                             m.get(k, 0), labels={"outcome": k}))
    except Exception as e:
        logger.debug("metrics_fabric_unavailable", error=str(e))
    return out


def _trace_collector_metrics() -> list[str]:
    out = []
    try:
        from app.services.trace_collector_service import trace_collector
        m = trace_collector.metrics()
        out.append(_help("helen_trace_events_total",
                         "Trace collector counters.", "counter"))
        for k in ("hops_recorded", "traces_started", "traces_completed",
                  "reaper_purged"):
            out.append(_line("helen_trace_events_total",
                             m.get(k, 0), labels={"outcome": k}))
    except Exception as e:
        logger.debug("metrics_trace_unavailable", error=str(e))
    return out


def _request_latency_metrics() -> list[str]:
    """Per-endpoint request latency histograms in Prometheus format.
    Buckets are cumulative (the standard Prometheus histogram contract):
    each `le` bucket counts requests at-or-below that threshold."""
    out = []
    try:
        from app.core.middleware import latency_tracker
        snap = latency_tracker.snapshot()
        if not snap:
            return out
        out.append(_help(
            "helen_request_duration_ms",
            "Per-endpoint HTTP request duration histogram (ms).",
            "histogram",
        ))
        for endpoint, stats in snap.items():
            cumulative = 0
            ep_label = endpoint.replace('"', '\\"')[:120]  # truncate long paths
            for le, count in stats["buckets"]:
                cumulative += count
                le_str = "+Inf" if le == float("inf") else str(le)
                out.append(
                    f'helen_request_duration_ms_bucket{{endpoint="{ep_label}",'
                    f'le="{le_str}"}} {cumulative}'
                )
            out.append(
                f'helen_request_duration_ms_count{{endpoint="{ep_label}"}} '
                f'{stats["count"]}'
            )
            out.append(
                f'helen_request_duration_ms_sum{{endpoint="{ep_label}"}} '
                f'{stats["sum_ms"]}'
            )
    except Exception as e:
        logger.debug("metrics_latency_unavailable", error=str(e))
    return out


def _bcrypt_queue_metrics() -> list[str]:
    """Auth-path saturation metrics. The bcrypt semaphore introduced to
    fix the megascale stampede has a fixed parallelism of CPU/2; surfacing
    its instantaneous depth lets operators correlate auth latency spikes
    with actual queue pressure rather than bcrypt CPU cost itself."""
    out = []
    try:
        from app.core.security import _get_bcrypt_sem, _BCRYPT_MAX_PARALLEL
        sem = _get_bcrypt_sem()
        # Semaphore.exposes _value (private but stable across CPython) for
        # the remaining capacity. waiters are tracked via _waiters deque.
        capacity_left = getattr(sem, "_value", _BCRYPT_MAX_PARALLEL)
        in_flight = max(0, _BCRYPT_MAX_PARALLEL - capacity_left)
        waiting = len(getattr(sem, "_waiters", []) or [])
        out.append(_help("helen_bcrypt_max_parallel",
                         "Configured bcrypt parallelism cap.", "gauge"))
        out.append(_line("helen_bcrypt_max_parallel", _BCRYPT_MAX_PARALLEL))
        out.append(_help("helen_bcrypt_in_flight",
                         "bcrypt operations currently running.", "gauge"))
        out.append(_line("helen_bcrypt_in_flight", in_flight))
        out.append(_help("helen_bcrypt_waiting",
                         "Auth callers waiting on the bcrypt semaphore.", "gauge"))
        out.append(_line("helen_bcrypt_waiting", waiting))
    except Exception as e:
        logger.debug("metrics_bcrypt_unavailable", error=str(e))
    return out


def _socket_io_metrics() -> list[str]:
    """Live socket roster + auth/registration counters. Cheap to compute
    on every scrape — no DB hit, just dict introspection on the in-memory
    presence service."""
    out = []
    try:
        from app.services.presence_service import presence_service
        active_sockets = len(getattr(presence_service, "_sid_user", {}))
        active_users = len(set((getattr(presence_service, "_sid_user", {}) or {}).values()))
        out.append(_help("helen_active_sockets_total",
                         "Distinct Socket.IO connections currently authenticated.",
                         "gauge"))
        out.append(_line("helen_active_sockets_total", active_sockets))
        out.append(_help("helen_active_users_total",
                         "Distinct users currently online (one user may have many sockets).",
                         "gauge"))
        out.append(_line("helen_active_users_total", active_users))
    except Exception as e:
        logger.debug("metrics_presence_unavailable", error=str(e))
    return out


async def _peer_state_metrics_async() -> list[str]:
    """Peer-approval state distribution. Shows how many peers are
    DISCOVERED / READY / WAITING etc. so admins can spot stuck approvals.

    Async because we're inside the fastapi async handler — running
    sync `run_until_complete` from there would deadlock on the event
    loop already in progress."""
    out: list[str] = []
    try:
        from app.db.session import async_session_factory
        from app.models.server_node import ServerNode
        from sqlalchemy import select as sa_select, func as sa_func
        async with async_session_factory() as db:
            result = await db.execute(
                sa_select(ServerNode.approval_status, sa_func.count(ServerNode.id))
                .group_by(ServerNode.approval_status)
            )
            counts = {row[0] or "unknown": row[1] for row in result.all()}
        if counts:
            out.append(_help("helen_peer_state_count",
                             "Peer approval-state distribution.", "gauge"))
            for state, n in counts.items():
                out.append(_line("helen_peer_state_count", n,
                                 labels={"state": state}))
    except Exception as e:
        logger.debug("metrics_peer_state_unavailable", error=str(e))
    return out


# ── Endpoint ────────────────────────────────────────────────────────


@router.get("", response_class=Response)
async def get_metrics(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    # Manual auth — see module docstring for the dual-gate rationale.
    if _is_public_metrics_allowed():
        pass
    else:
        expected_token = os.environ.get("HELEN_METRICS_TOKEN", "").strip()
        if expected_token:
            if not authorization or authorization != f"Bearer {expected_token}":
                raise HTTPException(status_code=401, detail="invalid metrics token")
        else:
            # Admin gate via JWT decode from the Authorization header.
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="bearer token required")
            token = authorization.removeprefix("Bearer ").strip()
            try:
                from app.core.security import decode_token
                payload = decode_token(token)
                uid = payload.get("sub")
                if not uid or payload.get("type") != "access":
                    raise HTTPException(status_code=401, detail="invalid token")
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=401, detail="metrics auth failed")
            user = (await db.execute(
                select(User).where(User.id == uid)
            )).scalar_one_or_none()
            if user is None or getattr(user, "role", None) != "admin":
                raise HTTPException(status_code=403, detail="admin role required")

    lines: list[str] = []
    sync_collectors = (
        _route_executor_metrics,
        _ack_manager_metrics,
        _broker_metrics,
        _priority_queue_metrics,
        _load_monitor_metrics,
        _fabric_subscribers_metrics,
        _trace_collector_metrics,
        _bcrypt_queue_metrics,
        _socket_io_metrics,
        _request_latency_metrics,
    )
    for collector in sync_collectors:
        block = collector()
        if block:
            lines.extend(block)
            lines.append("")
    # Async collector — peer-state needs a DB query, so it lives outside
    # the sync loop above and is awaited explicitly.
    block = await _peer_state_metrics_async()
    if block:
            lines.extend(block)
            lines.append("")  # blank line between metric families

    body = "\n".join(lines).strip() + "\n"
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
