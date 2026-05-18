"""
Phase 6 / Module AD — Prometheus metrics exporter wrapper.

Defines a *centralised* metrics registry of named families used across
Helen-Server. The actual `prometheus_client` dep is optional — when
missing, all helpers degrade to no-ops so calling code never breaks.

Existing Phase-5 metrics (in ``app.observability_legacy`` or similar)
are left untouched; the new families here cover Phase-6 scope.
"""
from __future__ import annotations

from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# Mostly used buckets for HTTP / DB / pubsub latencies (seconds).
LATENCY_BUCKETS = (
    0.001, 0.005, 0.010, 0.025, 0.050, 0.100,
    0.250, 0.500, 1.0, 2.5, 5.0, 10.0, 30.0,
)


_DISABLED = False
_FAMILIES: dict[str, Any] = {}
_REGISTRY: Any = None


def _ensure_init() -> bool:
    """Lazy-init the registry. Idempotent."""
    global _DISABLED, _REGISTRY
    if _DISABLED:
        return False
    if _REGISTRY is not None:
        return True
    try:
        from prometheus_client import CollectorRegistry  # type: ignore
        _REGISTRY = CollectorRegistry()
    except Exception as exc:
        logger.info("metrics: prometheus_client missing (%s); metrics disabled", exc)
        _DISABLED = True
        return False
    _register_all()
    return True


def _register_all() -> None:
    try:
        from prometheus_client import Counter, Gauge, Histogram  # type: ignore
    except Exception:                                               # pragma: no cover
        return

    # HTTP layer
    _FAMILIES["http_requests_total"] = Counter(
        "helen_http_requests_total",
        "Count of HTTP requests handled.",
        ["method", "route", "status_class"],
        registry=_REGISTRY,
    )
    _FAMILIES["http_request_duration_seconds"] = Histogram(
        "helen_http_request_duration_seconds",
        "Latency of HTTP requests in seconds.",
        ["method", "route"],
        buckets=LATENCY_BUCKETS,
        registry=_REGISTRY,
    )

    # Connections / sockets
    _FAMILIES["active_connections"] = Gauge(
        "helen_active_connections",
        "Currently open socket connections (HTTP + WebSocket).",
        ["transport"],
        registry=_REGISTRY,
    )
    _FAMILIES["socket_events_total"] = Counter(
        "helen_socket_events_total",
        "Total socket.io events handled.",
        ["event", "direction", "outcome"],
        registry=_REGISTRY,
    )

    # DB
    _FAMILIES["db_query_duration_seconds"] = Histogram(
        "helen_db_query_duration_seconds",
        "Duration of SQL queries in seconds.",
        ["operation"],
        buckets=LATENCY_BUCKETS,
        registry=_REGISTRY,
    )

    # Auth
    _FAMILIES["jwt_issued_total"] = Counter(
        "helen_jwt_issued_total",
        "JWT tokens issued.",
        ["kind"],
        registry=_REGISTRY,
    )
    _FAMILIES["jwt_rejected_total"] = Counter(
        "helen_jwt_rejected_total",
        "JWT tokens rejected.",
        ["reason"],
        registry=_REGISTRY,
    )

    # Bridges (Module Y)
    _FAMILIES["bridges_delivery_total"] = Counter(
        "helen_bridges_delivery_total",
        "Bridge messages delivered.",
        ["kind", "direction", "status"],
        registry=_REGISTRY,
    )

    # AI (Module Z)
    _FAMILIES["ai_tokens_used_total"] = Counter(
        "helen_ai_tokens_used_total",
        "AI provider tokens consumed.",
        ["provider", "model"],
        registry=_REGISTRY,
    )

    # Backups (Module AA)
    _FAMILIES["backup_duration_seconds"] = Histogram(
        "helen_backup_duration_seconds",
        "Time taken by backup jobs.",
        ["kind"],
        buckets=(1, 5, 15, 60, 120, 300, 600, 1800, 3600),
        registry=_REGISTRY,
    )

    # Webhooks (Module AF / existing)
    _FAMILIES["webhook_delivery_latency_seconds"] = Histogram(
        "helen_webhook_delivery_latency_seconds",
        "Latency of webhook delivery.",
        ["endpoint_kind"],
        buckets=LATENCY_BUCKETS,
        registry=_REGISTRY,
    )

    # Cluster
    _FAMILIES["cluster_nodes"] = Gauge(
        "helen_cluster_nodes",
        "Number of cluster nodes by status.",
        ["status"],
        registry=_REGISTRY,
    )
    _FAMILIES["cluster_is_leader"] = Gauge(
        "helen_cluster_is_leader",
        "1 if this process is the cluster leader, else 0.",
        registry=_REGISTRY,
    )

    # Security
    _FAMILIES["waf_blocks_total"] = Counter(
        "helen_waf_blocks_total",
        "Requests blocked by the WAF.",
        ["category"],
        registry=_REGISTRY,
    )
    _FAMILIES["ratelimit_rejected_total"] = Counter(
        "helen_ratelimit_rejected_total",
        "Requests rejected by the rate-limiter.",
        ["scope"],
        registry=_REGISTRY,
    )
    _FAMILIES["ids_events_total"] = Counter(
        "helen_ids_events_total",
        "Intrusion-detection events emitted.",
        ["kind", "action"],
        registry=_REGISTRY,
    )

    # Internal / housekeeping
    _FAMILIES["dlq_size"] = Gauge(
        "helen_dlq_size",
        "Current dead-letter queue depth.",
        registry=_REGISTRY,
    )


# ── public no-op friendly helpers ───────────────────────────


def counter_inc(name: str, value: float = 1.0, **labels: str) -> None:
    if not _ensure_init():
        return
    c = _FAMILIES.get(name)
    if c is None:
        return
    try:
        (c.labels(**labels) if labels else c).inc(value)
    except Exception:                                               # pragma: no cover
        pass


def gauge_set(name: str, value: float, **labels: str) -> None:
    if not _ensure_init():
        return
    g = _FAMILIES.get(name)
    if g is None:
        return
    try:
        (g.labels(**labels) if labels else g).set(value)
    except Exception:                                               # pragma: no cover
        pass


def histogram_observe(name: str, value: float, **labels: str) -> None:
    if not _ensure_init():
        return
    h = _FAMILIES.get(name)
    if h is None:
        return
    try:
        (h.labels(**labels) if labels else h).observe(value)
    except Exception:                                               # pragma: no cover
        pass
