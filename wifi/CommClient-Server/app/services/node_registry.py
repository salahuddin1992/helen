"""
Node registry — tracks every Helen-Server instance on the LAN.

Each node self-registers on startup with its hardware capabilities
(CPU cores, RAM, NIC speed, roles) and maintains a heartbeat with
live load metrics pulled from the control plane.

Single-node deployments end up with exactly one entry (self).
Multi-node deployments (future) gain entries when peers register via
UDP broadcast + HTTP /api/admin/placement/nodes/register.

The registry exposes `best_node_for(room_request)` which feeds the
placement scorer.
"""

from __future__ import annotations

import json
import platform
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import psutil
import structlog

logger = structlog.get_logger(__name__)

import os as _os
_DATA_DIR = Path(_os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_NODE_ID_FILE = _DATA_DIR / "node_id.txt"

# Nodes not heard from in this many seconds are marked DEAD and skipped
# by placement. Fresh heartbeats mark them ALIVE again.
NODE_STALE_SEC = 15
NODE_DEAD_SEC = 45


@dataclass
class NodeCapability:
    cpu_cores:      int
    ram_gb:         float
    nic_gbps:       float
    disk_ssd:       bool
    platform:       str
    version:        str


@dataclass
class NodeRoles:
    signaling:       bool = True
    messaging:       bool = True
    presence:        bool = True
    sfu:             bool = True
    relay:           bool = True
    recording:       bool = False
    file_transfer:   bool = True
    metrics:         bool = True


@dataclass
class NodeLoad:
    cpu_pct:         float = 0.0
    rss_pct:         float = 0.0
    nic_rx_mbps:     float = 0.0
    nic_tx_mbps:     float = 0.0
    active_sockets:  int = 0
    active_rooms:    int = 0
    active_calls:    int = 0
    phase:           str = "normal"


@dataclass
class NodeCapacity:
    """Derived limits — how much this node can hold, auto-computed from
    hardware specs unless the operator has overridden.

    Values are DERIVED from NodeCapability by `compute_capacity`. Overrides
    are stored alongside so UI can show both.
    """
    max_concurrent_sockets:      int = 0
    max_concurrent_rooms:        int = 0
    max_audio_participants:      int = 0   # total across all voice rooms
    max_video_participants:      int = 0   # total across all video rooms
    max_video_per_room:          int = 0   # hard cap for a single room
    max_broadcast_subscribers:   int = 0   # one-to-many fan-out cap
    file_upload_mbps_reserved:   int = 0   # NIC budget carved out for files
    overrides:                   dict = field(default_factory=dict)


@dataclass
class Node:
    node_id:        str
    host:           str
    port:           int
    self_node:      bool
    capability:     NodeCapability
    roles:          NodeRoles
    load:           NodeLoad
    last_heartbeat: float
    registered_at:  float
    capacity:       NodeCapacity = field(default_factory=NodeCapacity)
    extra:          dict = field(default_factory=dict)

    def is_fresh(self) -> bool:
        return (time.time() - self.last_heartbeat) < NODE_STALE_SEC

    def is_dead(self) -> bool:
        return (time.time() - self.last_heartbeat) > NODE_DEAD_SEC


def _persistent_node_id() -> str:
    """Return the same server_id that /api/discovery advertises.

    Using a single identity across discovery + node_registry + gossip
    avoids mismatch bugs where peer registration records one id and
    gossip packets carry another.
    """
    try:
        from app.services.discovery_service import get_server_id
        sid = get_server_id()
        if sid:
            return sid
    except Exception as e:
        logger.warning("node_id_discovery_lookup_failed", error=str(e))
    # Fallback to old behavior if discovery_service is unreachable.
    try:
        if _NODE_ID_FILE.is_file():
            v = _NODE_ID_FILE.read_text(encoding="utf-8").strip()
            if v:
                return v
    except Exception:
        pass
    nid = uuid.uuid4().hex[:16]
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _NODE_ID_FILE.write_text(nid, encoding="utf-8")
    except Exception as e:
        logger.warning("node_id_persist_failed", error=str(e))
    return nid


def _detect_capability() -> NodeCapability:
    cores = 1
    try:
        cores = psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 1
    except Exception:
        pass
    ram_gb = 1.0
    try:
        ram_gb = round(psutil.virtual_memory().total / 1024**3, 1)
    except Exception:
        pass
    # Rough NIC speed detection — psutil exposes speed on some platforms.
    nic_gbps = 1.0  # default 1Gbps fallback
    try:
        stats = psutil.net_if_stats()
        # Pick the fastest up-interface that isn't loopback.
        speeds = [s.speed for name, s in stats.items()
                  if s.isup and not name.lower().startswith(("lo", "loopback"))
                  and s.speed > 0]
        if speeds:
            nic_gbps = round(max(speeds) / 1000.0, 2)
    except Exception:
        pass
    disk_ssd = True  # assume SSD on modern hardware; refine later if needed
    return NodeCapability(
        cpu_cores=int(cores),
        ram_gb=ram_gb,
        nic_gbps=nic_gbps,
        disk_ssd=disk_ssd,
        platform=f"{platform.system()} {platform.release()}",
        version="1.0.0",
    )


def _detect_roles_from_config() -> NodeRoles:
    """Read server_roles.json; fall back to defaults when missing."""
    roles = NodeRoles()
    try:
        rf = _DATA_DIR / "server_roles.json"
        if rf.is_file():
            data = json.loads(rf.read_text(encoding="utf-8"))
            for key in ("signaling", "messaging", "presence", "sfu",
                        "relay", "recording", "file_transfer", "metrics"):
                if key in data and isinstance(data[key], dict):
                    setattr(roles, key, bool(data[key].get("enabled", True)))
    except Exception:
        pass
    return roles


class NodeRegistry:
    _singleton: "NodeRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nodes: dict[str, Node] = {}
        self._self_node_id = _persistent_node_id()
        self._register_self()

    @classmethod
    def instance(cls) -> "NodeRegistry":
        if cls._singleton is None:
            cls._singleton = NodeRegistry()
        return cls._singleton

    def _register_self(self) -> None:
        try:
            host = socket.gethostname()
        except Exception:
            host = "localhost"
        import os
        port = int(os.environ.get("PORT", 3000))
        cap = _detect_capability()
        overrides = self._load_capacity_overrides()
        n = Node(
            node_id=self._self_node_id,
            host=host,
            port=port,
            self_node=True,
            capability=cap,
            roles=_detect_roles_from_config(),
            load=NodeLoad(),
            last_heartbeat=time.time(),
            registered_at=time.time(),
            capacity=compute_capacity(cap, overrides),
        )
        with self._lock:
            self._nodes[n.node_id] = n
        logger.info("node_self_registered",
                    node_id=n.node_id, host=host, port=port,
                    cores=cap.cpu_cores, ram_gb=cap.ram_gb,
                    max_video_per_room=n.capacity.max_video_per_room)

    @staticmethod
    def _load_capacity_overrides() -> dict:
        try:
            f = _DATA_DIR / "node_capacity_overrides.json"
            if f.is_file():
                return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def save_capacity_overrides(self, overrides: dict) -> None:
        """Persist operator-overridden limits and recompute self capacity."""
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        (_DATA_DIR / "node_capacity_overrides.json").write_text(
            json.dumps(overrides, indent=2), encoding="utf-8")
        with self._lock:
            s = self._nodes.get(self._self_node_id)
            if s:
                s.capacity = compute_capacity(s.capability, overrides)

    # ── Public API ─────────────────────────────────────────────
    @property
    def self_node_id(self) -> str:
        return self._self_node_id

    def register_peer(
        self,
        node_id: str,
        host: str,
        port: int,
        capability: dict,
        roles: dict,
        capacity: Optional[dict] = None,
    ) -> Node:
        """Register a peer Helen-Server node."""
        cap = NodeCapability(
            cpu_cores=int(capability.get("cpu_cores", 1)),
            ram_gb=float(capability.get("ram_gb", 1.0)),
            nic_gbps=float(capability.get("nic_gbps", 1.0)),
            disk_ssd=bool(capability.get("disk_ssd", True)),
            platform=str(capability.get("platform", "?")),
            version=str(capability.get("version", "?")),
        )
        r = NodeRoles(**{k: bool(v) for k, v in roles.items()
                         if k in NodeRoles.__dataclass_fields__})
        now = time.time()
        with self._lock:
            existing = self._nodes.get(node_id)
            if existing and existing.self_node:
                return existing
            # Capacity: prefer what the peer advertised; fall back to
            # computing it ourselves from capability.
            if capacity and isinstance(capacity, dict):
                cap_obj = NodeCapacity(
                    max_concurrent_sockets=int(capacity.get("max_concurrent_sockets", 0)),
                    max_concurrent_rooms=int(capacity.get("max_concurrent_rooms", 0)),
                    max_audio_participants=int(capacity.get("max_audio_participants", 0)),
                    max_video_participants=int(capacity.get("max_video_participants", 0)),
                    max_video_per_room=int(capacity.get("max_video_per_room", 0)),
                    max_broadcast_subscribers=int(capacity.get("max_broadcast_subscribers", 0)),
                    file_upload_mbps_reserved=int(capacity.get("file_upload_mbps_reserved", 0)),
                )
            else:
                cap_obj = compute_capacity(cap)
            n = Node(
                node_id=node_id, host=host, port=port, self_node=False,
                capability=cap, roles=r, load=NodeLoad(),
                last_heartbeat=now,
                registered_at=existing.registered_at if existing else now,
                capacity=cap_obj,
            )
            self._nodes[node_id] = n
        logger.info("node_peer_registered",
                    node_id=node_id, host=host, cores=cap.cpu_cores)
        return n

    def heartbeat(self, node_id: str, load: dict) -> bool:
        """Update a node's load metrics + heartbeat timestamp."""
        with self._lock:
            n = self._nodes.get(node_id)
            if not n:
                return False
            n.load = NodeLoad(
                cpu_pct=float(load.get("cpu_pct", 0)),
                rss_pct=float(load.get("rss_pct", 0)),
                nic_rx_mbps=float(load.get("nic_rx_mbps", 0)),
                nic_tx_mbps=float(load.get("nic_tx_mbps", 0)),
                active_sockets=int(load.get("active_sockets", 0)),
                active_rooms=int(load.get("active_rooms", 0)),
                active_calls=int(load.get("active_calls", 0)),
                phase=str(load.get("phase", "normal")),
            )
            n.last_heartbeat = time.time()
        return True

    def refresh_self_load(self) -> None:
        """Pull live load from the control plane into self's NodeLoad.

        Called on a timer by the control plane tick so the self-entry
        stays fresh without a separate heartbeat path.
        """
        try:
            from app.services.control_plane import ControlPlane
            s = ControlPlane.instance().status()
            self.heartbeat(self._self_node_id, {
                "cpu_pct": s["inputs"]["cpu_p95"],
                "rss_pct": s["inputs"]["rss_p95"],
                "nic_rx_mbps": s["inputs"].get("nic_rx_mbps", 0),
                "nic_tx_mbps": s["inputs"].get("nic_tx_mbps", 0),
                "active_sockets": s["inputs"].get("active_sockets", 0),
                "active_rooms": len(s.get("rooms", [])),
                "active_calls": sum(1 for r in s.get("rooms", [])
                                    if r.get("kind") in ("voice", "video")),
                "phase": s["global"]["phase"],
            })
        except Exception as e:
            logger.debug("self_load_refresh_failed", error=str(e))

    def unregister(self, node_id: str) -> bool:
        with self._lock:
            n = self._nodes.get(node_id)
            if not n or n.self_node:
                return False
            self._nodes.pop(node_id, None)
        return True

    def nodes(self, include_dead: bool = False) -> list[Node]:
        with self._lock:
            nodes = list(self._nodes.values())
        if not include_dead:
            nodes = [n for n in nodes if not n.is_dead()]
        return nodes

    def node_dicts(self, include_dead: bool = False) -> list[dict]:
        out = []
        for n in self.nodes(include_dead=include_dead):
            d = {
                "node_id":         n.node_id,
                "host":            n.host,
                "port":            n.port,
                "self_node":       n.self_node,
                "capability":      asdict(n.capability),
                "roles":           asdict(n.roles),
                "load":            asdict(n.load),
                "capacity":        asdict(n.capacity),
                "last_heartbeat":  n.last_heartbeat,
                "registered_at":   n.registered_at,
                "fresh":           n.is_fresh(),
                "dead":            n.is_dead(),
            }
            d["strength"] = compute_strength(n.capability)
            d["headroom"] = compute_headroom(n.load)
            d["score"]    = d["strength"] * d["headroom"]
            # Live utilization vs capacity
            ut = {}
            if n.capacity.max_concurrent_sockets > 0:
                ut["sockets_pct"] = round(
                    100 * n.load.active_sockets / n.capacity.max_concurrent_sockets, 1)
            if n.capacity.max_concurrent_rooms > 0:
                ut["rooms_pct"] = round(
                    100 * n.load.active_rooms / n.capacity.max_concurrent_rooms, 1)
            d["utilization"] = ut
            out.append(d)
        out.sort(key=lambda x: x["score"], reverse=True)
        return out


# ── Scoring primitives (used by placement.py and exposed here for UI) ─
def compute_strength(cap: NodeCapability) -> float:
    """Hardware weight. Scaled so a 4-core / 8GB / 1Gbps box = ~4.0."""
    return round(
        0.4 * cap.cpu_cores +
        0.3 * cap.ram_gb +
        0.2 * (cap.nic_gbps * 10) +     # nic_gbps 1 = weight 2, 10Gbps = weight 20
        0.1 * (5 if cap.disk_ssd else 1),
        2,
    )


def compute_capacity(cap: NodeCapability,
                     overrides: Optional[dict] = None) -> NodeCapacity:
    """Derive concurrent limits from hardware specs.

    LAN-MAX formulas — raised to the absolute ceiling per operator
    instruction "صعد الافتراض الاتصال بالسيرفرات الى اعلى حد". On a pure
    LAN, sockets are dirt-cheap: the kernel handles the multiplexing,
    user space sees them only on heartbeat ticks, and the bandwidth
    is effectively switched-fabric-free. The only real bottlenecks are
    file descriptors and per-room CPU on active media.

      max_concurrent_sockets  ≈ cores × 3000       (WS connections — doubled)
      max_concurrent_rooms    ≈ cores × 12         (active rooms — doubled)
      max_audio_participants  ≈ cores × 100        (total voice — doubled)
      max_video_participants  ≈ min(cores × 12,    (720p per-leg CPU — doubled)
                                    nic_gbps × 80) (NIC fan-out — doubled)
      max_video_per_room      ≈ min(cores × 8, 160)(hard cap per room — doubled)
      max_broadcast_subs      ≈ nic_gbps × 500     (one-to-many — 2.5× — LAN
                                                   switch broadcast is free)
      file_upload_mbps_res    ≈ nic_gbps × 600     (60% of NIC for files)

    Operator overrides win unconditionally — the YAML in the admin panel
    can pin any field if the workload differs from these heuristics.
    """
    overrides = overrides or {}
    cores = max(1, cap.cpu_cores)
    nic = max(0.1, cap.nic_gbps)
    c = NodeCapacity(
        max_concurrent_sockets    = int(cores * 3000),
        max_concurrent_rooms      = int(cores * 12),
        max_audio_participants    = int(cores * 100),
        max_video_participants    = int(min(cores * 12, nic * 80)),
        max_video_per_room        = int(min(cores * 8, 160)),
        max_broadcast_subscribers = int(nic * 500),
        file_upload_mbps_reserved = int(nic * 600),
    )
    # Apply operator overrides, field by field. Invalid keys silently ignored.
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if hasattr(c, k) and isinstance(v, (int, float)):
                setattr(c, k, int(v))
        c.overrides = {k: v for k, v in overrides.items()
                       if hasattr(c, k) and isinstance(v, (int, float))}
    return c


def compute_headroom(load: NodeLoad) -> float:
    """Multiplicative factor in [0, 1]. 1.0 = fully idle, 0 = saturated."""
    cpu_left = max(0.0, 1.0 - load.cpu_pct / 100.0)
    rss_left = max(0.0, 1.0 - load.rss_pct / 100.0)
    phase_penalty = {
        "normal":    1.0,
        "degraded":  0.7,
        "emergency": 0.1,
        "frozen":    0.0,
    }.get(load.phase, 1.0)
    return round(cpu_left * rss_left * phase_penalty, 3)


def get_registry() -> NodeRegistry:
    return NodeRegistry.instance()
