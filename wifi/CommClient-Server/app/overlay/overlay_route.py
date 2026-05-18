"""OverlayRoute — resolution of paths inside an overlay.

The route resolver wraps the graph's path queries, validates that
the result respects the configured ``max_route_hops``, and turns
the abstract overlay path into a concrete ``physical_chain`` of
peer_ids that the routing_strategy / cluster_mesh can dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.overlay.overlay_config import get_config
from app.overlay.overlay_exceptions import OverlayRouteError
from app.overlay.overlay_graph import OverlayGraph


@dataclass
class OverlayRoute:
    overlay_name:   str
    src_id:         str
    dst_id:         str
    nodes:          list[str] = field(default_factory=list)
    physical_chain: list[str] = field(default_factory=list)
    cost:           float = 0.0

    @property
    def hop_count(self) -> int:
        return max(0, len(self.nodes) - 1)

    def to_dict(self) -> dict:
        return {
            "overlay_name":  self.overlay_name,
            "src_id":        self.src_id,
            "dst_id":        self.dst_id,
            "nodes":         list(self.nodes),
            "physical_chain": list(self.physical_chain),
            "cost":          self.cost,
            "hop_count":     self.hop_count,
        }


def _to_physical(graph: OverlayGraph, nodes: list[str]) -> list[str]:
    out: list[str] = []
    for nid in nodes:
        n = graph.node(nid)
        out.append(n.peer_id if (n and n.peer_id) else nid)
    return out


def resolve_shortest(
    graph: OverlayGraph,
    src_id: str,
    dst_id: str,
) -> OverlayRoute:
    """BFS shortest path. Raises OverlayRouteError when no path exists
    or when the path exceeds the configured max_route_hops."""
    cfg = get_config()
    nodes = graph.shortest_path(src_id, dst_id)
    if not nodes:
        raise OverlayRouteError(f"no path {src_id!r} → {dst_id!r}")
    if len(nodes) - 1 > cfg.max_route_hops:
        raise OverlayRouteError(
            f"path too long: {len(nodes) - 1} > {cfg.max_route_hops}"
        )
    return OverlayRoute(
        overlay_name=graph.overlay_name,
        src_id=src_id, dst_id=dst_id,
        nodes=nodes,
        physical_chain=_to_physical(graph, nodes),
        cost=float(len(nodes) - 1),
    )


def resolve_k_shortest(
    graph: OverlayGraph,
    src_id: str,
    dst_id: str,
    k: int = 4,
) -> list[OverlayRoute]:
    """Up to K weight-aware paths. Empty list = unreachable."""
    cfg = get_config()
    paths = graph.k_shortest_paths(src_id, dst_id, k=k,
                                    max_depth=cfg.max_route_hops + 1)
    return [
        OverlayRoute(
            overlay_name=graph.overlay_name,
            src_id=src_id, dst_id=dst_id,
            nodes=p,
            physical_chain=_to_physical(graph, p),
            cost=float(len(p) - 1),
        )
        for p in paths if (len(p) - 1) <= cfg.max_route_hops
    ]
