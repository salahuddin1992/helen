"""
Pathfinder — weighted shortest-path resolver over the aggregated topology.

Helen's overlay graphs each have their own intra-overlay BFS; this module is
the *global* shortest-path solver — it spans every layer (physical, overlay,
federation, application) and produces hop-by-hop latency/transport for the
admin Topology Visualizer's "Trace path" panel.

Implementation
--------------
* **Dijkstra** with a binary-heap min-priority queue (``heapq``).
* **Weight model** (default ``rtt``) — ``rtt_ms`` plus an additive penalty
  derived from packet loss so that a high-loss link is automatically
  deprioritised. The penalty is bounded so a single 100 % loss link doesn't
  poison the path search.
* **Tie-breaking** — when two edges have identical weights the lower
  ``layer`` rank wins (physical → overlay → application → federation), which
  matches operator intuition.
* **Pluggable** — pass ``weight='hops'`` for unweighted BFS-equivalent, or
  any ``Callable[[TopologyLink], float]`` for custom scoring.

The output is intentionally JSON-friendly: each hop is a flat dict so the
admin UI can render it directly in a table.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Union

import structlog

from app.services.topology.aggregator import (
    LAYER_APPLICATION,
    LAYER_FEDERATION,
    LAYER_OVERLAY,
    LAYER_PHYSICAL,
    TopologyGraph,
    TopologyLink,
)

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

# Lower = higher priority during tie-breaks.
LAYER_RANK = {
    LAYER_PHYSICAL:    0,
    LAYER_OVERLAY:     1,
    LAYER_APPLICATION: 2,
    LAYER_FEDERATION:  3,
}

# A reasonable default for links that haven't reported latency yet — we
# refuse to pick a totally-unmeasured link over a measured one by accident.
DEFAULT_UNKNOWN_RTT_MS = 50.0

# Cap on the packet-loss penalty so one bad link doesn't dominate the search.
PACKET_LOSS_PENALTY_MAX_MS = 500.0


# ─────────────────────────────────────────────────────────────
# Result models
# ─────────────────────────────────────────────────────────────


@dataclass
class PathHop:
    """One edge in the resolved path + cumulative latency to that hop."""

    src:                str
    dst:                str
    transport:          str
    layer:              str
    rtt_ms:             float
    packet_loss_pct:    float
    cumulative_rtt_ms:  float
    metadata:           dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "src":               self.src,
            "dst":               self.dst,
            "transport":         self.transport,
            "layer":             self.layer,
            "rtt_ms":            round(self.rtt_ms, 3),
            "packet_loss_pct":   round(self.packet_loss_pct, 3),
            "cumulative_rtt_ms": round(self.cumulative_rtt_ms, 3),
            "metadata":          dict(self.metadata),
        }


@dataclass
class PathResult:
    """The complete resolved path between two nodes."""

    src:           str
    dst:           str
    hops:          list[PathHop] = field(default_factory=list)
    total_rtt_ms:  float = 0.0
    hop_count:     int = 0
    found:         bool = False
    weight_metric: str = "rtt"

    def to_dict(self) -> dict[str, Any]:
        return {
            "src":           self.src,
            "dst":           self.dst,
            "found":         self.found,
            "hop_count":     self.hop_count,
            "total_rtt_ms":  round(self.total_rtt_ms, 3),
            "weight_metric": self.weight_metric,
            "hops":          [h.to_dict() for h in self.hops],
        }


# ─────────────────────────────────────────────────────────────
# Pathfinder
# ─────────────────────────────────────────────────────────────


WeightFn = Callable[[TopologyLink], float]


class Pathfinder:
    """Dijkstra path resolver over a ``TopologyGraph``."""

    # ── Weight helpers ────────────────────────────────────────

    @staticmethod
    def weight_rtt(link: TopologyLink) -> float:
        """Latency + packet-loss penalty (default)."""
        rtt = link.rtt_ms if link.rtt_ms > 0 else DEFAULT_UNKNOWN_RTT_MS
        loss = max(0.0, min(100.0, link.packet_loss_pct)) / 100.0
        penalty = loss * PACKET_LOSS_PENALTY_MAX_MS
        return rtt + penalty

    @staticmethod
    def weight_hops(_link: TopologyLink) -> float:
        return 1.0

    @staticmethod
    def weight_loss(link: TopologyLink) -> float:
        return 1.0 + (link.packet_loss_pct / 100.0) * 10.0

    @classmethod
    def resolve_weight_fn(
        cls, weight: Union[str, WeightFn] = "rtt"
    ) -> WeightFn:
        if callable(weight):
            return weight
        weight = (weight or "rtt").lower()
        if weight == "rtt":
            return cls.weight_rtt
        if weight in ("hop", "hops", "uniform"):
            return cls.weight_hops
        if weight == "loss":
            return cls.weight_loss
        raise ValueError(f"unknown weight metric: {weight!r}")

    # ── Core algorithm ────────────────────────────────────────

    @classmethod
    def find_path(
        cls,
        graph: TopologyGraph,
        src: str,
        dst: str,
        *,
        weight: Union[str, WeightFn] = "rtt",
    ) -> PathResult:
        """
        Dijkstra over ``graph``.

        Returns ``PathResult`` with ``found=False`` when either endpoint is
        absent or there is no route between them. Self-loops trivially
        return an empty hop list with ``found=True``.
        """
        weight_fn = cls.resolve_weight_fn(weight)
        metric_name = (
            weight if isinstance(weight, str) else getattr(weight, "__name__", "fn")
        )
        result = PathResult(src=src, dst=dst, weight_metric=metric_name)

        node_ids: set[str] = {n.id for n in graph.nodes}
        if src not in node_ids or dst not in node_ids:
            return result

        if src == dst:
            result.found = True
            return result

        # adjacency: src → list[link]
        adj: dict[str, list[TopologyLink]] = {}
        for e in graph.edges:
            adj.setdefault(e.src, []).append(e)
            # Treat application/overlay/federation edges as undirected too —
            # the rendered graph is functionally bidirectional.
            adj.setdefault(e.dst, []).append(TopologyLink(
                src=e.dst, dst=e.src,
                transport=e.transport,
                layer=e.layer,
                rtt_ms=e.rtt_ms,
                throughput_msg_per_sec=e.throughput_msg_per_sec,
                packet_loss_pct=e.packet_loss_pct,
                weight=e.weight,
                metadata=e.metadata,
            ))

        # Dijkstra
        dist: dict[str, float] = {src: 0.0}
        prev: dict[str, tuple[str, TopologyLink]] = {}
        # heap entries — (cost, layer_rank_tiebreak, counter, node_id)
        counter = 0
        heap: list[tuple[float, int, int, str]] = [(0.0, 0, counter, src)]

        while heap:
            cost, _lr, _ct, u = heapq.heappop(heap)
            if u == dst:
                break
            if cost > dist.get(u, float("inf")):
                continue

            for link in adj.get(u, []):
                w = weight_fn(link)
                if w < 0:
                    continue
                new_cost = cost + w
                if new_cost < dist.get(link.dst, float("inf")):
                    dist[link.dst] = new_cost
                    prev[link.dst] = (u, link)
                    counter += 1
                    heapq.heappush(heap, (
                        new_cost,
                        LAYER_RANK.get(link.layer, 99),
                        counter,
                        link.dst,
                    ))

        if dst not in prev:
            return result

        # Walk back from dst → src.
        hops: list[PathHop] = []
        cur = dst
        while cur != src:
            parent, link = prev[cur]
            hops.append(PathHop(
                src=link.src,
                dst=link.dst,
                transport=link.transport,
                layer=link.layer,
                rtt_ms=link.rtt_ms,
                packet_loss_pct=link.packet_loss_pct,
                cumulative_rtt_ms=0.0,  # filled in below
                metadata=dict(link.metadata),
            ))
            cur = parent
        hops.reverse()

        cumulative = 0.0
        for h in hops:
            cumulative += h.rtt_ms if h.rtt_ms > 0 else DEFAULT_UNKNOWN_RTT_MS
            h.cumulative_rtt_ms = cumulative

        result.hops = hops
        result.total_rtt_ms = cumulative
        result.hop_count = len(hops)
        result.found = True
        return result
