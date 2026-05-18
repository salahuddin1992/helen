"""
Network Topology Visualizer — service package.

Exposes:
    TopologyAggregator       — multi-source graph builder (server + router +
                                federation + overlay + P2P + clients).
    Pathfinder               — RTT/loss-weighted Dijkstra over the aggregated
                                graph.
    TopologyActions          — node-level operations (ping, traceroute,
                                drain, restart, failover) with an in-memory
                                job registry.
    TopologyWebSocketManager — fan-out broadcaster for live topology events.

Importing this package is side-effect free; the singletons are lazily
instantiated through their factory helpers and only become active when the
admin topology router is exercised.
"""

from __future__ import annotations

from app.services.topology.aggregator import (
    TopologyAggregator,
    get_topology_aggregator,
)
from app.services.topology.pathfinder import Pathfinder, PathHop, PathResult
from app.services.topology.actions import (
    TopologyActions,
    TopologyJob,
    get_topology_actions,
)
from app.services.topology.ws_stream import (
    TopologyWebSocketManager,
    get_topology_ws_manager,
)

__all__ = [
    "TopologyAggregator",
    "get_topology_aggregator",
    "Pathfinder",
    "PathHop",
    "PathResult",
    "TopologyActions",
    "TopologyJob",
    "get_topology_actions",
    "TopologyWebSocketManager",
    "get_topology_ws_manager",
]
