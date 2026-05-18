"""Topology dashboard — high-level aggregator for the admin UI.

Combines:
  * Live graph stats (nodes / links / components / bridges).
  * Recent diff (delta between last two snapshots).
  * NIC routing table.
  * Hot links (top 5 by latency).

One JSON endpoint, no recompute work — pulls cached snapshots only.
"""

from __future__ import annotations


def render_dashboard() -> dict:
    out: dict = {}

    # Topology graph stats.
    try:
        from app.topology import get_topology_manager
        mgr = get_topology_manager()
        g = mgr.graph
        out["graph_stats"] = g.stats()
        out["bridges"] = [b.to_dict() for b in g.bridges()]
        # Top-10 fastest links.
        fastest = sorted(
            (L for L in g.all_links() if L.latency_ms > 0),
            key=lambda L: L.latency_ms,
        )[:10]
        out["fastest_links"] = [
            {
                "src":        L.src_id,
                "dst":        L.dst_id,
                "type":       L.link_type.value,
                "latency_ms": round(L.latency_ms, 2),
            }
            for L in fastest
        ]
    except Exception as e:
        out["graph_error"] = str(e)[:100]

    # Recent diff.
    try:
        from app.monitoring.topology_snapshot import get_topology_capturer
        out["snapshot_diff"] = get_topology_capturer().diff()
    except Exception:
        out["snapshot_diff"] = None

    # NIC routing.
    try:
        from app.services.nic_routing_table import get_nic_routing_table
        nrt = get_nic_routing_table()
        nrt.refresh()
        out["nic_routing"] = nrt.snapshot()
    except Exception:
        out["nic_routing"] = {}

    # Partition state for context.
    try:
        from app.services.partition_detector import get_partition_state
        out["partition"] = get_partition_state().snapshot()
    except Exception:
        out["partition"] = {}

    return out
