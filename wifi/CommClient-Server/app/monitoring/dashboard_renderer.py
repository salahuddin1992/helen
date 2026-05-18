"""Dashboard renderer — text + JSON views of the live cluster state.

The renderer pulls from health_checker, metrics_collector,
alert_manager, latency_tracker, and topology_snapshot to produce
operator-friendly views. No I/O of its own — it's a pure compose
step.
"""

from __future__ import annotations

import time
from typing import Any

from app.monitoring.monitoring_exceptions import DashboardRenderError


# ── Aggregator ──────────────────────────────────────────────────


def aggregate_state() -> dict:
    """Pull the latest state from every monitoring sub-component."""
    state: dict[str, Any] = {"ts": time.time()}
    try:
        from app.monitoring.health_checker import get_health_checker
        state["health"] = get_health_checker().latest()
    except Exception as e:
        state["health"] = {"error": str(e)}
    try:
        from app.monitoring.metrics_collector import get_metrics_collector
        state["metrics"] = get_metrics_collector().latest()
    except Exception as e:
        state["metrics"] = {"error": str(e)}
    try:
        from app.monitoring.alert_manager import get_alert_manager
        state["alerts"] = get_alert_manager().all_states()
    except Exception as e:
        state["alerts"] = {"error": str(e)}
    try:
        from app.monitoring.latency_tracker import get_latency_tracker
        state["latency"] = get_latency_tracker().all_stats()
    except Exception as e:
        state["latency"] = {"error": str(e)}
    try:
        from app.monitoring.topology_snapshot import get_topology_capturer
        state["topology"] = {
            "latest": get_topology_capturer().latest(),
            "diff":   get_topology_capturer().diff(),
        }
    except Exception as e:
        state["topology"] = {"error": str(e)}
    return state


# ── Renderers ────────────────────────────────────────────────────


def render_json() -> dict:
    return aggregate_state()


def render_text() -> str:
    """Plain-text dashboard — friendly to terminals + log shippers."""
    try:
        s = aggregate_state()
    except Exception as e:
        raise DashboardRenderError(f"aggregate_state failed: {e}")

    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("HELEN MONITORING DASHBOARD")
    lines.append("=" * 64)

    # Health
    h = s.get("health") or {}
    h_ok = h.get("ok")
    lines.append(f"[health] ok={h_ok}  ({h.get('ok_count', 0)} / {h.get('total_checks', 0)})")
    for name, r in (h.get("checks") or {}).items():
        glyph = "✓" if r.get("ok") else "✗"
        lines.append(f"  {glyph} {name:<18s} {r.get('detail', '')}")

    # Alerts
    al = s.get("alerts") or {}
    firing = [n for n, st in al.items() if st and st.get("firing")]
    lines.append("")
    lines.append(f"[alerts] firing={len(firing)} of {len(al)}")
    for n in firing:
        st = al.get(n) or {}
        lines.append(f"  ⚠ {n}  {st.get('detail', '')}")

    # Topology
    topo = (s.get("topology") or {}).get("latest")
    if topo:
        lines.append("")
        lines.append(f"[topology] nodes={topo.get('node_count')} "
                     f"links={topo.get('link_count')} "
                     f"components={topo.get('components')} "
                     f"bridges={len(topo.get('bridges') or [])}")

    # Metrics summary
    metrics = s.get("metrics") or {}
    bp = (metrics.get("backpressure") or {})
    pt = (metrics.get("partition") or {})
    lines.append("")
    lines.append(
        f"[load] backpressure={bp.get('level', 'n/a')} "
        f"saturation={bp.get('saturation', 'n/a')}  "
        f"majority={pt.get('is_majority', 'n/a')}"
    )

    # Latency
    lat = s.get("latency") or {}
    if lat:
        lines.append("")
        lines.append("[latency p95]")
        for op, st in lat.items():
            if st.get("count", 0) > 0:
                lines.append(f"  {op:<20s} count={st['count']:>5d} "
                             f"p95={st['p95']:>7.2f}ms")

    return "\n".join(lines)


def render_mermaid_summary() -> str:
    """Compact mermaid pie chart — alerts + health for embedding in
    Markdown."""
    s = aggregate_state()
    h = s.get("health") or {}
    al = s.get("alerts") or {}
    ok = h.get("ok_count", 0)
    fail = max(0, h.get("total_checks", 0) - ok)
    firing = sum(1 for st in al.values() if st and st.get("firing"))
    cleared = max(0, len(al) - firing)
    return (
        "pie title Helen Health\n"
        f"  \"Healthy checks\" : {ok}\n"
        f"  \"Failing checks\" : {fail}\n"
        f"  \"Firing alerts\"  : {firing}\n"
        f"  \"Quiet alerts\"   : {cleared}\n"
    )
