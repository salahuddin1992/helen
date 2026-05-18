"""Subnet model + IP/CIDR utilities.

A Subnet is a broadcast domain — every node whose IP falls inside
the same CIDR can reach every other resident directly via UDP
broadcast. The topology graph uses this to:

  * Group nodes for visualisation.
  * Identify cross-subnet links (always BRIDGE / PROXY / TUNNEL).
  * Detect bridge candidates (nodes resident in ≥ 2 subnets).
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Subnet:
    """A CIDR block + the node_ids that live inside it."""
    cidr:        str                              # "192.168.1.0/24"
    gateway:     Optional[str] = None
    nodes:       set[str] = field(default_factory=set)
    is_local:    bool = False
    discovered_at: float = field(default_factory=time.time)
    extra:       dict = field(default_factory=dict)

    # ── Membership ────────────────────────────────────────

    def contains_ip(self, ip: str) -> bool:
        try:
            return ipaddress.ip_address(ip) in ipaddress.ip_network(
                self.cidr, strict=False,
            )
        except ValueError:
            return False

    def add_node(self, node_id: str) -> None:
        self.nodes.add(node_id)

    def remove_node(self, node_id: str) -> None:
        self.nodes.discard(node_id)

    # ── Serialisation ─────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        d["nodes"] = sorted(self.nodes)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Subnet":
        return cls(
            cidr=str(data["cidr"]),
            gateway=data.get("gateway"),
            nodes=set(data.get("nodes") or []),
            is_local=bool(data.get("is_local") or False),
            discovered_at=float(data.get("discovered_at") or time.time()),
            extra=dict(data.get("extra") or {}),
        )


# ── Helpers ─────────────────────────────────────────────────────


def infer_subnet(ip: str, default_prefix: int = 24) -> Optional[str]:
    """Return the /{prefix} CIDR that an IP belongs to.

    Examples:
      192.168.1.42         → "192.168.1.0/24"
      10.0.0.5             → "10.0.0.0/24"
      172.16.7.99 (pf=16)  → "172.16.0.0/16"

    Returns None for malformed input or for non-IPv4 addresses (we
    keep IPv6 out of scope for the topology graph for now).
    """
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if not isinstance(addr, ipaddress.IPv4Address):
        return None
    network = ipaddress.ip_network(
        f"{ip}/{default_prefix}", strict=False,
    )
    return str(network)


def is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def same_subnet(a_ip: str, b_ip: str, prefix: int = 24) -> bool:
    """True iff the two IPs share a /{prefix} block."""
    sa = infer_subnet(a_ip, prefix)
    sb = infer_subnet(b_ip, prefix)
    return bool(sa) and sa == sb


def list_local_subnets() -> list[str]:
    """Best-effort enumeration of subnets the host is currently
    attached to — uses psutil if available, falls back to a single
    inferred subnet from socket.gethostbyname."""
    out: list[str] = []
    try:
        import psutil
        import socket
        for nic, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family.name in ("AF_INET",):
                    if is_loopback(a.address):
                        continue
                    cidr = infer_subnet(a.address)
                    if cidr and cidr not in out:
                        out.append(cidr)
    except Exception:
        try:
            import socket
            cidr = infer_subnet(socket.gethostbyname(socket.gethostname()))
            if cidr:
                out.append(cidr)
        except Exception:
            pass
    return out
