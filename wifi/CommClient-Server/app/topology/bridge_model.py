"""Bridge-role node specialisation.

A "bridge" in Helen terminology is a *peer* (Helen-Server) that
happens to sit on more than one subnet — typically because the
operator has plugged in a second NIC (Ethernet + WiFi, USB-tether,
fiber link). A bridge is the most valuable node in a multi-router
deployment: it's the only thing that lets a peer in subnet-A reach
a peer in subnet-B without an external rendezvous.

The Bridge model carries:

  * ``subnets``       — every CIDR this bridge is resident in.
  * ``host_aliases``  — IP addresses across NICs (the same machine
                        is reachable on each).
  * ``forwarding``    — whether the operator has explicitly enabled
                        cross-subnet forwarding (we don't auto-bridge
                        — operator opt-in via env / config).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.topology.node_model import Node, NodeType


@dataclass
class Bridge(Node):
    """Multi-NIC peer that can forward traffic between subnets."""
    subnets:        list[str] = field(default_factory=list)
    host_aliases:   list[str] = field(default_factory=list)
    forwarding:     bool = True

    def __post_init__(self) -> None:
        # A Node entering as PEER but having multiple subnets is a
        # bridge — promote its node_type so visualisers render it
        # correctly.
        if len(self.subnets) >= 2 and self.node_type is NodeType.PEER:
            self.node_type = NodeType.BRIDGE
        # A bridge always carries the bridge=True extra flag so the
        # relay chain prioritises it.
        self.extra.setdefault("bridge", True)

    def can_forward_between(self, subnet_a: str, subnet_b: str) -> bool:
        return (
            self.forwarding
            and subnet_a in self.subnets
            and subnet_b in self.subnets
            and subnet_a != subnet_b
        )

    def add_subnet(self, cidr: str) -> None:
        if cidr and cidr not in self.subnets:
            self.subnets.append(cidr)

    def add_alias(self, ip: str) -> None:
        if ip and ip not in self.host_aliases:
            self.host_aliases.append(ip)

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["subnets"] = list(self.subnets)
        d["host_aliases"] = list(self.host_aliases)
        d["forwarding"] = self.forwarding
        d["node_kind"] = "bridge"
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Bridge":
        base = Node.from_dict(data)
        return cls(
            **{k: getattr(base, k) for k in (
                "node_id", "node_type", "host", "port", "subnet",
                "nics", "cluster_id", "roles", "capabilities",
                "is_self", "last_seen", "extra",
            )},
            subnets=list(data.get("subnets") or []),
            host_aliases=list(data.get("host_aliases") or []),
            forwarding=bool(data.get("forwarding", True)),
        )
