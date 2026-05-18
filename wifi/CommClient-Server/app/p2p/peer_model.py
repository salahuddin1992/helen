"""Peer model — the P2P-layer view of a remote node.

Distinct from ``app.topology.Node`` (which is a graph vertex) and
``services.peer_registry.PeerRecord`` (which is the discovery
record). The Peer model is the *behavioural* one — what we know
*about* the peer for the purposes of opening sessions, scoring it,
and routing messages through it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum


class PeerRole(str, Enum):
    NORMAL      = "normal"        # plain participant
    SUPER       = "super"         # high-capacity, popular intermediate
    RELAY       = "relay"         # passive forwarder
    PROXY       = "proxy"         # active HTTP forwarder
    BRIDGE      = "bridge"        # multi-NIC, cross-subnet
    DISCOVERY   = "discovery"     # broadcast announcer
    DHT         = "dht"           # Kademlia table holder
    FEDERATION  = "federation"    # cross-cluster gateway
    STORAGE     = "storage"       # holds replicated records
    MONITORING  = "monitoring"    # metrics collector
    NAT_TRAVERSAL = "nat_traversal"  # punch / tunnel coordinator
    GATEWAY     = "gateway"       # WAN egress
    BOOTSTRAP   = "bootstrap"     # well-known seed
    QUARANTINED = "quarantined"   # bad behaviour, isolated


@dataclass
class Peer:
    """In-memory peer record for the p2p layer."""
    peer_id:    str
    role:       PeerRole
    host:       str
    port:       int = 0
    cluster_id: str = "default"
    pubkey:     str = ""                              # optional Ed25519
    capabilities: dict = field(default_factory=dict)  # cores/ram/nic_gbps
    roles:      set[str] = field(default_factory=set) # capability flags
    score:      float = 0.5
    last_seen:  float = field(default_factory=time.time)
    bridge_subnets: list[str] = field(default_factory=list)
    extra:      dict = field(default_factory=dict)

    # ── Identity by peer_id only ──────────────────────────

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Peer) and self.peer_id == other.peer_id

    def __hash__(self) -> int:
        return hash(self.peer_id)

    # ── Predicates ─────────────────────────────────────────

    def is_bridge(self) -> bool:
        return self.role is PeerRole.BRIDGE or len(self.bridge_subnets) >= 2

    def is_quarantined(self) -> bool:
        return self.role is PeerRole.QUARANTINED

    def is_routable(self) -> bool:
        return not self.is_quarantined() and bool(self.host)

    def freshness_age_sec(self) -> float:
        return max(0.0, time.time() - self.last_seen)

    def is_fresh(self, max_age_sec: float = 60.0) -> bool:
        return self.freshness_age_sec() <= max_age_sec

    # ── Serialisation ───────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        d["role"] = self.role.value
        d["roles"] = sorted(self.roles)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Peer":
        return cls(
            peer_id=str(data["peer_id"]),
            role=PeerRole(data.get("role", PeerRole.NORMAL.value)),
            host=str(data.get("host") or ""),
            port=int(data.get("port") or 0),
            cluster_id=str(data.get("cluster_id") or "default"),
            pubkey=str(data.get("pubkey") or ""),
            capabilities=dict(data.get("capabilities") or {}),
            roles=set(data.get("roles") or []),
            score=float(data.get("score") or 0.5),
            last_seen=float(data.get("last_seen") or time.time()),
            bridge_subnets=list(data.get("bridge_subnets") or []),
            extra=dict(data.get("extra") or {}),
        )
