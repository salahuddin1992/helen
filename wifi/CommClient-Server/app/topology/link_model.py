"""Link model — directed edge between two nodes in the topology.

A link records *how* two nodes are connected, not just *that* they
are. Multiple links between the same pair are allowed when they
represent different transport classes (e.g. one LAN link via the
primary NIC, another BRIDGE link via a USB-tethered second NIC).

Metrics on the link feed into ``multipath_router.score_route`` —
``path_health`` is the live source of truth for latency, but the
Link copy is what visualisers + persistence use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class LinkType(str, Enum):
    """Transport class — answers "how does the packet travel?".

    The same pair of nodes can have *several* links of different
    types active at once; multipath routing picks among them.
    """
    LAN_DIRECT  = "lan_direct"     # same subnet, primary NIC
    LAN_ALIAS   = "lan_alias"      # same node, alternate interface
    BRIDGE      = "bridge"         # cross-subnet via multi-NIC peer
    PROXY       = "proxy"          # HTTP relay through a peer
    RELAY       = "relay"          # blind byte forwarder
    TUNNEL      = "tunnel"         # reverse WS tunnel via rendezvous
    HOLE_PUNCH  = "hole_punch"     # UDP NAT traversal
    FEDERATION  = "federation"     # cross-cluster HMAC channel
    DHT         = "dht"             # Kademlia overlay edge


@dataclass
class Link:
    """Directed edge ``src_id → dst_id`` with live metrics."""
    src_id:         str
    dst_id:         str
    link_type:      LinkType
    latency_ms:     float = 0.0
    bandwidth_mbps: float = 0.0
    packet_loss:    float = 0.0
    last_seen:      float = field(default_factory=time.time)
    last_success:   float = 0.0
    fail_count:     int = 0
    score:          float = 0.0
    extra:          dict = field(default_factory=dict)

    # ── Identity ──────────────────────────────────────────

    @property
    def key(self) -> tuple[str, str, str]:
        """A link is uniquely identified by (src, dst, type) — the
        type matters because two LAN_DIRECT and BRIDGE links between
        the same pair are *different* paths."""
        return (self.src_id, self.dst_id, self.link_type.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Link) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    # ── Metric updates ────────────────────────────────────

    def record_success(self, latency_ms: float,
                       bandwidth_mbps: Optional[float] = None) -> None:
        # EWMA with α = 0.3 — same constant used by path_health for
        # consistency.
        if self.latency_ms == 0:
            self.latency_ms = latency_ms
        else:
            self.latency_ms = 0.3 * latency_ms + 0.7 * self.latency_ms
        if bandwidth_mbps is not None:
            if self.bandwidth_mbps == 0:
                self.bandwidth_mbps = bandwidth_mbps
            else:
                self.bandwidth_mbps = (
                    0.3 * bandwidth_mbps + 0.7 * self.bandwidth_mbps
                )
        self.last_success = time.time()
        self.last_seen = self.last_success
        self.fail_count = 0

    def record_failure(self) -> None:
        self.fail_count += 1
        self.last_seen = time.time()
        # Crude packet-loss estimate: 1 - successes / (successes + fails).
        # We don't track successes here, so use a fail-count proxy.
        self.packet_loss = min(1.0, 0.05 * self.fail_count)

    def is_alive(self, max_age_sec: float = 60.0) -> bool:
        return (time.time() - self.last_seen) <= max_age_sec

    # ── Serialisation ─────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        d["link_type"] = self.link_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Link":
        return cls(
            src_id=str(data["src_id"]),
            dst_id=str(data["dst_id"]),
            link_type=LinkType(data.get("link_type", LinkType.LAN_DIRECT.value)),
            latency_ms=float(data.get("latency_ms") or 0.0),
            bandwidth_mbps=float(data.get("bandwidth_mbps") or 0.0),
            packet_loss=float(data.get("packet_loss") or 0.0),
            last_seen=float(data.get("last_seen") or time.time()),
            last_success=float(data.get("last_success") or 0.0),
            fail_count=int(data.get("fail_count") or 0),
            score=float(data.get("score") or 0.0),
            extra=dict(data.get("extra") or {}),
        )
