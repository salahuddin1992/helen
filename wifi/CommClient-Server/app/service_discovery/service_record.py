"""ServiceRecord — the durable model of a registered service.

Every entity that wants to be discoverable (Helen-Server itself,
relays, signaling, media gateways, federation gateways, NAT
helpers) registers a ServiceRecord. The fields are deliberate:

  * Identity        — (service_id, service_type, server_id)
  * Endpoint        — (host, port, protocol, public_url)
  * Locality        — (cluster_id, region, zone)
  * Health          — (status, last_heartbeat_at, ttl_sec)
  * Capacity        — (max_capacity, current_load, score_hint)
  * Capabilities    — flexible dict (codecs, tls, e2ee_supported, …)
  * Trust signals   — (signature, signed_at, public_key_fingerprint)

Records are JSON-serialisable so they can be persisted, gossipped,
and federated without separate marshalling code paths.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class ServiceType(str, Enum):
    PEER             = "peer"             # Helen-Server (full app)
    RELAY            = "relay"            # passive byte forwarder
    PROXY            = "proxy"            # active HTTP forwarder
    BRIDGE           = "bridge"           # multi-NIC peer
    SIGNALING        = "signaling"        # WebRTC signaling server
    MEDIA_GATEWAY    = "media_gateway"    # SFU / MCU
    DHT_NODE         = "dht_node"         # Kademlia
    FEDERATION       = "federation"       # cross-cluster gateway
    RENDEZVOUS       = "rendezvous"       # NAT-traversal hub
    DISCOVERY        = "discovery"        # discovery beacon (mDNS/UDP)
    STORAGE          = "storage"          # holds replicated records
    OVERLAY          = "overlay"          # overlay-network endpoint


class ServiceStatus(str, Enum):
    REGISTERING = "registering"   # awaiting first heartbeat
    HEALTHY     = "healthy"
    DEGRADED    = "degraded"      # alive but reporting load > 80%
    UNHEALTHY   = "unhealthy"     # last heartbeat missed grace
    DRAINING    = "draining"      # graceful stop in progress
    DEAD        = "dead"          # past TTL, awaiting reaper


@dataclass
class ServiceRecord:
    service_id:    str = field(default_factory=lambda: uuid.uuid4().hex)
    service_type:  ServiceType = ServiceType.PEER
    server_id:     str = ""              # owning Helen-Server node id
    host:          str = ""
    port:          int = 0
    protocol:      str = "http"          # http / https / udp / ws
    public_url:    str = ""              # optional reverse-tunnel URL

    cluster_id:    str = "default"
    region:        str = "default"
    zone:          str = "default"

    status:        ServiceStatus = ServiceStatus.REGISTERING
    registered_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    ttl_sec:       float = 60.0

    # Capacity hints (free-form numbers — used by scoring).
    max_capacity:   int = 0
    current_load:   int = 0
    capacity_pct:   float = 0.0

    # Latency hint advertised at registration (ms).
    advertised_latency_ms: float = 0.0

    capabilities:  dict = field(default_factory=dict)
    tags:          set[str] = field(default_factory=set)

    # Trust signals.
    signature:     str = ""
    signed_at:     float = 0.0
    pubkey_fingerprint: str = ""

    # Identity by service_id only.
    def __eq__(self, other: object) -> bool:
        return isinstance(other, ServiceRecord) and self.service_id == other.service_id

    def __hash__(self) -> int:
        return hash(self.service_id)

    # ── Predicates ─────────────────────────────────────────

    def is_alive(self, grace_sec: float = 15.0,
                 now: Optional[float] = None) -> bool:
        n = now if now is not None else time.time()
        return (n - self.last_heartbeat_at) <= (self.ttl_sec + grace_sec)

    def is_healthy(self) -> bool:
        return (self.status == ServiceStatus.HEALTHY
                and self.is_alive())

    def is_dead(self, grace_sec: float = 15.0,
                now: Optional[float] = None) -> bool:
        return not self.is_alive(grace_sec=grace_sec, now=now)

    def remaining_capacity(self) -> int:
        if self.max_capacity <= 0:
            return 0
        return max(0, self.max_capacity - self.current_load)

    def headroom_pct(self) -> float:
        if self.max_capacity <= 0:
            return 100.0
        return max(0.0, 100.0 * (1.0 - self.current_load / self.max_capacity))

    # ── Mutation ───────────────────────────────────────────

    def beat(self, *, current_load: int | None = None,
             status: ServiceStatus | None = None,
             ts: float | None = None) -> None:
        self.last_heartbeat_at = ts if ts is not None else time.time()
        if current_load is not None:
            self.current_load = int(current_load)
            if self.max_capacity > 0:
                self.capacity_pct = 100.0 * self.current_load / self.max_capacity
        if status is not None:
            self.status = status
        elif self.status == ServiceStatus.REGISTERING:
            self.status = ServiceStatus.HEALTHY

    # ── Serialisation ─────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        d["service_type"] = self.service_type.value
        d["status"] = self.status.value
        d["tags"] = sorted(self.tags)
        d["remaining_capacity"] = self.remaining_capacity()
        d["headroom_pct"] = round(self.headroom_pct(), 2)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ServiceRecord":
        return cls(
            service_id=str(data.get("service_id") or uuid.uuid4().hex),
            service_type=ServiceType(
                data.get("service_type", ServiceType.PEER.value)
            ),
            server_id=str(data.get("server_id") or ""),
            host=str(data.get("host") or ""),
            port=int(data.get("port") or 0),
            protocol=str(data.get("protocol") or "http"),
            public_url=str(data.get("public_url") or ""),
            cluster_id=str(data.get("cluster_id") or "default"),
            region=str(data.get("region") or "default"),
            zone=str(data.get("zone") or "default"),
            status=ServiceStatus(
                data.get("status", ServiceStatus.REGISTERING.value)
            ),
            registered_at=float(data.get("registered_at") or time.time()),
            last_heartbeat_at=float(data.get("last_heartbeat_at") or time.time()),
            ttl_sec=float(data.get("ttl_sec") or 60.0),
            max_capacity=int(data.get("max_capacity") or 0),
            current_load=int(data.get("current_load") or 0),
            capacity_pct=float(data.get("capacity_pct") or 0.0),
            advertised_latency_ms=float(data.get("advertised_latency_ms") or 0.0),
            capabilities=dict(data.get("capabilities") or {}),
            tags=set(data.get("tags") or []),
            signature=str(data.get("signature") or ""),
            signed_at=float(data.get("signed_at") or 0.0),
            pubkey_fingerprint=str(data.get("pubkey_fingerprint") or ""),
        )
