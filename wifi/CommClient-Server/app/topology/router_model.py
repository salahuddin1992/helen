"""Router-role node specialisation.

In the abstract topology, a "router" is the device that *bridges
subnets at L3* — a home WiFi/Ethernet router, an enterprise switch
with VLAN routing, or a cloud egress gateway. Helen-Server itself
is not normally a router (it's a peer), but a multi-NIC machine
running Helen-Server *can* act as one when configured.

The Router model carries:

  * ``downstream_subnets`` — CIDRs the router is the gateway for.
  * ``uplink``             — the upstream subnet / gateway.
  * ``nat_type``           — symmetric / full-cone / restricted /
                             port-restricted / open. Affects which
                             NAT-traversal route classes work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.topology.node_model import Node, NodeType


class NATType(str, Enum):
    OPEN              = "open"               # public IP, no NAT
    FULL_CONE         = "full_cone"          # easiest to traverse
    RESTRICTED        = "restricted"
    PORT_RESTRICTED   = "port_restricted"
    SYMMETRIC         = "symmetric"          # hardest, hole-punch fails
    UNKNOWN           = "unknown"


@dataclass
class Router(Node):
    """A Node specialisation with router-specific fields."""
    downstream_subnets: list[str] = field(default_factory=list)
    uplink:             Optional[str] = None
    nat_type:           NATType = NATType.UNKNOWN
    public_ip:          Optional[str] = None

    def __post_init__(self) -> None:
        if self.node_type is None or self.node_type == NodeType.PEER:
            self.node_type = NodeType.ROUTER

    def supports_hole_punch(self) -> bool:
        """Hole-punch only works for non-symmetric NATs."""
        return self.nat_type in (
            NATType.OPEN, NATType.FULL_CONE,
            NATType.RESTRICTED, NATType.PORT_RESTRICTED,
        )

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["downstream_subnets"] = list(self.downstream_subnets)
        d["uplink"] = self.uplink
        d["nat_type"] = self.nat_type.value
        d["public_ip"] = self.public_ip
        d["node_kind"] = "router"
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Router":
        base = Node.from_dict(data)
        return cls(
            **{k: getattr(base, k) for k in (
                "node_id", "node_type", "host", "port", "subnet",
                "nics", "cluster_id", "roles", "capabilities",
                "is_self", "last_seen", "extra",
            )},
            downstream_subnets=list(data.get("downstream_subnets") or []),
            uplink=data.get("uplink"),
            nat_type=NATType(data.get("nat_type", NATType.UNKNOWN.value)),
            public_ip=data.get("public_ip"),
        )
