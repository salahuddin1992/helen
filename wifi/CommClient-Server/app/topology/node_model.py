"""Node model — the atomic unit of the topology graph.

A Node is *anything* that participates in the mesh: clients, peers,
routers, bridges, dedicated relays. Each carries enough metadata to
let routers + visualisers reason about it without reaching back into
``services/`` for live state.

The dataclass is deliberately serialisable to/from JSON so the
``topology_store`` persistence and the federation gossip can ship
nodes as-is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class NodeType(str, Enum):
    """Functional role of a node in the topology.

    A single Helen-Server typically plays multiple roles
    simultaneously (PEER + DISCOVERY + RELAY + DHT). The NodeType
    here is the *primary* role we file the node under for graph
    rendering; the full set lives in ``Node.roles``.
    """
    CLIENT     = "client"          # end-user device (PC / phone)
    PEER       = "peer"            # full Helen-Server
    ROUTER     = "router"          # IP router / gateway
    BRIDGE     = "bridge"          # multi-NIC peer crossing subnets
    DISCOVERY  = "discovery"       # broadcast / mDNS advertiser
    RELAY      = "relay"           # passive byte forwarder
    PROXY      = "proxy"           # active HTTP forwarder
    FEDERATION = "federation"      # cross-cluster gateway
    DHT        = "dht"             # Kademlia DHT node
    RENDEZVOUS = "rendezvous"      # NAT-traversal hub


@dataclass
class Node:
    """A topology node — graph vertex.

    Equality is by ``node_id`` only; two records describing the same
    node may differ in liveness fields without being unequal.
    """
    node_id:    str
    node_type:  NodeType
    host:       str
    port:       int = 0
    subnet:     Optional[str] = None              # "192.168.1.0/24"
    nics:       list[str] = field(default_factory=list)
    cluster_id: str = "default"
    roles:      set[str] = field(default_factory=set)
    capabilities: dict = field(default_factory=dict)   # cores, ram, nic_gbps
    is_self:    bool = False
    last_seen:  float = field(default_factory=time.time)
    extra:      dict = field(default_factory=dict)     # bridge=True, etc.

    # ── Equality / hash by node_id only ────────────────────

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Node) and self.node_id == other.node_id

    def __hash__(self) -> int:
        return hash(self.node_id)

    # ── Convenience predicates ─────────────────────────────

    def is_bridge(self) -> bool:
        if self.node_type is NodeType.BRIDGE:
            return True
        return bool(self.extra.get("bridge")) or len(self.nics) >= 2

    def is_router(self) -> bool:
        return self.node_type is NodeType.ROUTER

    def is_client(self) -> bool:
        return self.node_type is NodeType.CLIENT

    def is_peer(self) -> bool:
        return self.node_type is NodeType.PEER

    def freshness_age_sec(self, now: Optional[float] = None) -> float:
        n = now if now is not None else time.time()
        return max(0.0, n - self.last_seen)

    def is_fresh(self, max_age_sec: float = 30.0,
                 now: Optional[float] = None) -> bool:
        return self.freshness_age_sec(now) <= max_age_sec

    # ── Serialisation ──────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        d["node_type"] = self.node_type.value
        d["roles"] = sorted(self.roles)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Node":
        return cls(
            node_id=str(data["node_id"]),
            node_type=NodeType(data.get("node_type", NodeType.PEER.value)),
            host=str(data.get("host") or ""),
            port=int(data.get("port") or 0),
            subnet=data.get("subnet"),
            nics=list(data.get("nics") or []),
            cluster_id=str(data.get("cluster_id") or "default"),
            roles=set(data.get("roles") or []),
            capabilities=dict(data.get("capabilities") or {}),
            is_self=bool(data.get("is_self") or False),
            last_seen=float(data.get("last_seen") or time.time()),
            extra=dict(data.get("extra") or {}),
        )
