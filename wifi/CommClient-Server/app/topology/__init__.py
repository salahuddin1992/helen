"""Helen network topology — pure data model layer.

This package is *additive*: it sits alongside ``app/services/`` and
exposes a structured graph representation of the cluster that the
existing services can read from / write to.

Module map
----------
    node_model         — Node, NodeType
    link_model         — Link, LinkType
    subnet_model       — Subnet detection from CIDR / IP
    router_model       — Router-role node specialisation
    bridge_model       — Bridge-role node (multi-NIC)
    topology_graph     — Graph + traversal algorithms
    topology_store     — JSON persistence
    topology_manager   — Singleton coordinator (entry point)
    topology_visualizer — ASCII / mermaid renderers

Public API:

    from app.topology import topology_manager
    snap = topology_manager.snapshot()
    paths = topology_manager.k_shortest_paths(src_id, dst_id, k=4)
"""

from app.topology.node_model import Node, NodeType                # noqa: F401
from app.topology.link_model import Link, LinkType                # noqa: F401
from app.topology.subnet_model import Subnet, infer_subnet        # noqa: F401
from app.topology.topology_graph import TopologyGraph             # noqa: F401
from app.topology.topology_manager import (                       # noqa: F401
    get_topology_manager,
    start_topology_manager,
    stop_topology_manager,
)
