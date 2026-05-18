"""
Prometheus-format metrics export.

Aggregates live state from every mesh subsystem (path_health,
trust_score, partition_detector, backpressure, cluster_time,
multipath_router, audit_replication, replication_manager) into the
text-based exposition format Prometheus / VictoriaMetrics scrapes.

Endpoint: ``GET /api/cluster/metrics`` (public, no auth — same
philosophy as Kubernetes ``/metrics``: numbers only, no secrets).

The format is hand-rolled (no prometheus_client dep) so we don't
add another runtime dependency to the bundled Helen-Server.exe.
That keeps the build under 18 MB and avoids version-skew with
whatever scrape stack the operator runs.
"""

from __future__ import annotations

import time
from typing import Iterable


# ── Generic helpers ─────────────────────────────────────────────


def _line(name: str, value: float, labels: dict | None = None,
          help_text: str = "", metric_type: str = "gauge") -> Iterable[str]:
    """Emit one metric (with HELP/TYPE only the first time we see
    the name in a single response; deduplication is the caller's
    concern)."""
    if help_text:
        yield f"# HELP {name} {help_text}"
    if metric_type:
        yield f"# TYPE {name} {metric_type}"
    label_str = ""
    if labels:
        # Python 3.10/3.11 reject backslashes inside f-string expression
        # parts; pre-escape into a plain str first, then interpolate.
        def _escape(v: object) -> str:
            return str(v).replace("\\", "\\\\").replace('"', '\\"')
        parts = [f'{k}="{_escape(v)}"' for k, v in labels.items()]
        label_str = "{" + ",".join(parts) + "}"
    yield f"{name}{label_str} {value}"


def _emitted_help(seen: set, name: str) -> bool:
    if name in seen:
        return True
    seen.add(name)
    return False


# ── Subsystem collectors ────────────────────────────────────────


def _collect_path_health(out: list, seen: set) -> None:
    try:
        from app.services.path_health import get_path_health
        snap = get_path_health().snapshot()
    except Exception:
        return
    name = "helen_path_latency_ms"
    if not _emitted_help(seen, name):
        out.append(f"# HELP {name} EWMA latency per (host:port) in milliseconds")
        out.append(f"# TYPE {name} gauge")
    for p in snap.get("paths", []):
        out.append(f'{name}{{key="{p["key"]}"}} {p["latency_ms"]}')

    name2 = "helen_path_in_cooldown"
    if not _emitted_help(seen, name2):
        out.append(f"# HELP {name2} 1 if path is in cooldown after recent failure")
        out.append(f"# TYPE {name2} gauge")
    for p in snap.get("paths", []):
        out.append(
            f'{name2}{{key="{p["key"]}"}} {1 if p["in_cooldown"] else 0}'
        )


def _collect_trust(out: list, seen: set) -> None:
    try:
        from app.services.trust_score import get_trust_db
        rows = get_trust_db().list_top(limit=500)
    except Exception:
        return
    name = "helen_peer_trust_score"
    if not _emitted_help(seen, name):
        out.append(f"# HELP {name} Persistent trust score per peer (0..1)")
        out.append(f"# TYPE {name} gauge")
    for r in rows:
        sid = r["server_id"][:24]
        out.append(f'{name}{{server_id="{sid}"}} {r["score"]}')


def _collect_partition(out: list, seen: set) -> None:
    try:
        from app.services.partition_detector import get_partition_state
        snap = get_partition_state().snapshot()
    except Exception:
        return
    out.append("# HELP helen_partition_is_majority 1 if local node is in the majority partition")
    out.append("# TYPE helen_partition_is_majority gauge")
    out.append(f"helen_partition_is_majority {1 if snap['is_majority'] else 0}")

    out.append("# HELP helen_partition_fresh_peers Visible fresh peer count (incl. self)")
    out.append("# TYPE helen_partition_fresh_peers gauge")
    out.append(f"helen_partition_fresh_peers {snap['fresh_count']}")

    out.append("# HELP helen_partition_high_water High-water peer count (cluster size memory)")
    out.append("# TYPE helen_partition_high_water gauge")
    out.append(f"helen_partition_high_water {snap['high_water']}")


def _collect_backpressure(out: list, seen: set) -> None:
    try:
        from app.services.backpressure import get_backpressure
        snap = get_backpressure().snapshot()
    except Exception:
        return
    out.append("# HELP helen_backpressure_saturation Current saturation score 0..1")
    out.append("# TYPE helen_backpressure_saturation gauge")
    out.append(f"helen_backpressure_saturation {snap['saturation']}")

    out.append("# HELP helen_backpressure_level 0=normal, 1=degraded, 2=rejected")
    out.append("# TYPE helen_backpressure_level gauge")
    level_int = {"normal": 0, "degraded": 1, "rejected": 2}.get(snap["level"], 0)
    out.append(f"helen_backpressure_level {level_int}")


def _collect_cluster_time(out: list, seen: set) -> None:
    try:
        from app.services.cluster_time import get_cluster_time
        snap = get_cluster_time().snapshot()
    except Exception:
        return
    out.append("# HELP helen_cluster_time_offset_sec Cluster-consensus offset from local clock")
    out.append("# TYPE helen_cluster_time_offset_sec gauge")
    out.append(f"helen_cluster_time_offset_sec {snap['offset_sec']}")


def _collect_audit(out: list, seen: set) -> None:
    try:
        from app.services.audit_replication import get_audit_replicator
        head = get_audit_replicator().head()
    except Exception:
        return
    out.append("# HELP helen_audit_chain_seq Current head sequence of the audit chain")
    out.append("# TYPE helen_audit_chain_seq counter")
    out.append(f"helen_audit_chain_seq {head['seq']}")


def _collect_multipath(out: list, seen: set) -> None:
    try:
        from app.services.multipath_router import snapshot as mp_snap
        snap = mp_snap()
    except Exception:
        return
    out.append("# HELP helen_multipath_route_count Total routes in the route table")
    out.append("# TYPE helen_multipath_route_count gauge")
    out.append(f"helen_multipath_route_count {len(snap.get('routes', []))}")

    # Per-route-type counts.
    by_type: dict[str, int] = {}
    for r in snap.get("routes", []):
        rt = r.get("route_type", "unknown")
        by_type[rt] = by_type.get(rt, 0) + 1
    out.append("# HELP helen_multipath_routes_by_type Route count per route_type")
    out.append("# TYPE helen_multipath_routes_by_type gauge")
    for rt, n in by_type.items():
        out.append(f'helen_multipath_routes_by_type{{route_type="{rt}"}} {n}')

    # In-cooldown count.
    cool = sum(1 for r in snap.get("routes", []) if r.get("in_cooldown"))
    out.append("# HELP helen_multipath_routes_cooldown Routes currently in failure cooldown")
    out.append("# TYPE helen_multipath_routes_cooldown gauge")
    out.append(f"helen_multipath_routes_cooldown {cool}")


def _collect_node_capacity(out: list, seen: set) -> None:
    try:
        from app.services.node_registry import get_registry
        reg = get_registry()
        peers = reg.nodes(include_dead=True)
    except Exception:
        return
    out.append("# HELP helen_cluster_node_count Known cluster node count")
    out.append("# TYPE helen_cluster_node_count gauge")
    fresh = sum(1 for n in peers if n.is_fresh())
    out.append(f'helen_cluster_node_count{{state="fresh"}} {fresh}')
    out.append(f'helen_cluster_node_count{{state="total"}} {len(peers)}')

    # Self capacity.
    me = next((n for n in peers if n.self_node), None)
    if me:
        out.append("# HELP helen_self_max_concurrent_sockets Self-advertised socket capacity")
        out.append("# TYPE helen_self_max_concurrent_sockets gauge")
        out.append(f"helen_self_max_concurrent_sockets {me.capacity.max_concurrent_sockets}")

        out.append("# HELP helen_self_active_sockets Live socket count")
        out.append("# TYPE helen_self_active_sockets gauge")
        out.append(f"helen_self_active_sockets {me.load.active_sockets}")


def _collect_overlay(out: list, seen: set) -> None:
    try:
        from app.overlay import get_overlay_manager
        snap = get_overlay_manager().snapshot().get("registry", {})
    except Exception:
        return
    out.append("# HELP helen_overlay_count Number of registered overlays")
    out.append("# TYPE helen_overlay_count gauge")
    out.append(f"helen_overlay_count {snap.get('count', 0)}")


def _collect_p2p(out: list, seen: set) -> None:
    try:
        from app.p2p.peer_registry import get_p2p_registry
        snap = get_p2p_registry().snapshot()
    except Exception:
        return
    out.append("# HELP helen_p2p_peer_count P2P-layer peer count")
    out.append("# TYPE helen_p2p_peer_count gauge")
    out.append(f'helen_p2p_peer_count{{state="all"}} {snap.get("count", 0)}')
    out.append(f'helen_p2p_peer_count{{state="fresh"}} {snap.get("fresh", 0)}')
    out.append(f'helen_p2p_peer_count{{state="bridges"}} {snap.get("bridges", 0)}')
    out.append(f'helen_p2p_peer_count{{state="quarantined"}} {snap.get("quarantined", 0)}')


def _collect_resilience(out: list, seen: set) -> None:
    try:
        from app.resilience import get_resilience_manager
        snap = get_resilience_manager().snapshot()
    except Exception:
        return
    bp = snap.get("breaker", {})
    rq = snap.get("retry_queue", {})
    out.append("# HELP helen_resilience_breaker_count Open circuit breakers count")
    out.append("# TYPE helen_resilience_breaker_count gauge")
    out.append(f'helen_resilience_breaker_count {bp.get("count", 0)}')
    out.append("# HELP helen_resilience_retry_pending Pending entries in retry queue")
    out.append("# TYPE helen_resilience_retry_pending gauge")
    out.append(f'helen_resilience_retry_pending {rq.get("pending", 0)}')
    deg = snap.get("degraded", {})
    level_n = {"normal": 0, "degraded": 1, "emergency": 2}.get(
        deg.get("level", "normal"), 0,
    )
    out.append("# HELP helen_resilience_degraded_level 0=normal 1=degraded 2=emergency")
    out.append("# TYPE helen_resilience_degraded_level gauge")
    out.append(f"helen_resilience_degraded_level {level_n}")


def _collect_nat(out: list, seen: set) -> None:
    try:
        from app.nat import get_nat_manager
        snap = get_nat_manager().snapshot()
    except Exception:
        return
    sessions = snap.get("sessions", {})
    out.append("# HELP helen_nat_session_count Active NAT-traversal sessions")
    out.append("# TYPE helen_nat_session_count gauge")
    out.append(f'helen_nat_session_count {sessions.get("count", 0)}')
    detector = snap.get("detector", {})
    out.append("# HELP helen_nat_type Detected NAT type as label")
    out.append("# TYPE helen_nat_type gauge")
    out.append(f'helen_nat_type{{type="{detector.get("type", "unknown")}"}} 1')


def _collect_topology(out: list, seen: set) -> None:
    try:
        from app.topology import get_topology_manager
        stats = get_topology_manager().snapshot().get("stats", {})
    except Exception:
        return
    out.append("# HELP helen_topology_node_count Topology graph node count")
    out.append("# TYPE helen_topology_node_count gauge")
    out.append(f'helen_topology_node_count {stats.get("node_count", 0)}')
    out.append("# HELP helen_topology_link_count Topology graph link count")
    out.append("# TYPE helen_topology_link_count gauge")
    out.append(f'helen_topology_link_count {stats.get("link_count", 0)}')
    out.append("# HELP helen_topology_components Connected components count")
    out.append("# TYPE helen_topology_components gauge")
    out.append(f'helen_topology_components {stats.get("components", 0)}')


# ── Top-level export ────────────────────────────────────────────


def render_prometheus() -> str:
    """Return the full prometheus-format text exposition."""
    out: list[str] = []
    seen: set[str] = set()

    out.append(f"# Helen-Server metrics, exposed at unix={int(time.time())}")
    _collect_path_health(out, seen)
    _collect_trust(out, seen)
    _collect_partition(out, seen)
    _collect_backpressure(out, seen)
    _collect_cluster_time(out, seen)
    _collect_audit(out, seen)
    _collect_multipath(out, seen)
    _collect_node_capacity(out, seen)
    _collect_overlay(out, seen)
    _collect_p2p(out, seen)
    _collect_resilience(out, seen)
    _collect_nat(out, seen)
    _collect_topology(out, seen)

    return "\n".join(out) + "\n"
