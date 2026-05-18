"""
TopologyAggregator — multi-source network graph builder.

The aggregator merges live state from every layer of the Helen stack into a
single graph object that the admin Topology Visualizer can render:

    +---------------------+
    | distributed_system  |  → cluster mesh nodes (server, router peers)
    +---------------------+
    | p2p (peer_registry) |  → P2P peers (relay, super, bridge, dht, …)
    +---------------------+
    | p2p.federation      |  → foreign-cluster peers
    +---------------------+
    | overlay.registry    |  → overlay nodes + links per overlay name
    +---------------------+
    | overlay.session     |  → ephemeral conversation contexts
    +---------------------+
    | p2p.dht             |  → DHT routing-table snapshot
    +---------------------+
    | services.monitoring |  → connected clients / agents (ConnectionRegistry)
    +---------------------+

Every sub-source is optional: if a service is not enabled the aggregator
returns an empty slice for that layer and marks it with a
``<service>_disabled: true`` flag in the response envelope. The router never
sees a 500 because one dependency happens to be missing.

The full graph is cached in-memory with a 5-second TTL — multiple concurrent
``/topology/graph`` calls coalesce on the same build coroutine via an
``asyncio.Lock``.
"""

from __future__ import annotations

import asyncio
import platform
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

# Logical layers — the UI uses these to colour-band the graph.
LAYER_PHYSICAL    = "physical"
LAYER_OVERLAY     = "overlay"
LAYER_APPLICATION = "application"
LAYER_FEDERATION  = "federation"

# Canonical node types.
NODE_TYPE_SERVER          = "server"
NODE_TYPE_ROUTER          = "router"
NODE_TYPE_CLIENT          = "client"
NODE_TYPE_AGENT           = "agent"
NODE_TYPE_RENDEZVOUS      = "rendezvous"
NODE_TYPE_FEDERATION_PEER = "federation_peer"
NODE_TYPE_RELAY           = "relay"

ALL_NODE_TYPES = {
    NODE_TYPE_SERVER,
    NODE_TYPE_ROUTER,
    NODE_TYPE_CLIENT,
    NODE_TYPE_AGENT,
    NODE_TYPE_RENDEZVOUS,
    NODE_TYPE_FEDERATION_PEER,
    NODE_TYPE_RELAY,
}

CACHE_TTL_SEC = 5.0


# ─────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────


@dataclass
class TopologyNode:
    """A vertex in the aggregated topology graph."""

    id:            str
    type:          str
    layer:         str
    hostname:      str = ""
    ip:            str = ""
    version:       str = ""
    uptime_sec:    float = 0.0
    cpu:           float = 0.0          # 0.0 – 100.0 %
    mem:           float = 0.0          # 0.0 – 100.0 %
    disk:          float = 0.0          # 0.0 – 100.0 %
    conn_count:    int = 0
    status:        str = "unknown"      # up | down | degraded | unknown
    neighbours:    list[str] = field(default_factory=list)
    tags:          list[str] = field(default_factory=list)
    metadata:      dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id":          self.id,
            "type":        self.type,
            "layer":       self.layer,
            "hostname":    self.hostname,
            "ip":          self.ip,
            "version":     self.version,
            "uptime_sec":  round(self.uptime_sec, 2),
            "cpu":         round(self.cpu, 2),
            "mem":         round(self.mem, 2),
            "disk":        round(self.disk, 2),
            "conn_count":  self.conn_count,
            "status":      self.status,
            "neighbours":  list(self.neighbours),
            "tags":        list(self.tags),
            "metadata":    dict(self.metadata),
        }


@dataclass
class TopologyLink:
    """A directed edge in the aggregated topology graph."""

    src:                    str
    dst:                    str
    transport:              str = "tcp"
    layer:                  str = LAYER_PHYSICAL
    rtt_ms:                 float = 0.0
    throughput_msg_per_sec: float = 0.0
    packet_loss_pct:        float = 0.0
    weight:                 float = 1.0
    metadata:               dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.transport)

    def to_dict(self) -> dict[str, Any]:
        return {
            "src":                    self.src,
            "dst":                    self.dst,
            "transport":              self.transport,
            "layer":                  self.layer,
            "rtt_ms":                 round(self.rtt_ms, 3),
            "throughput_msg_per_sec": round(self.throughput_msg_per_sec, 2),
            "packet_loss_pct":        round(self.packet_loss_pct, 3),
            "weight":                 round(self.weight, 4),
            "metadata":               dict(self.metadata),
        }


@dataclass
class TopologyGraph:
    """Full aggregated graph + per-source availability flags."""

    nodes:         list[TopologyNode] = field(default_factory=list)
    edges:         list[TopologyLink] = field(default_factory=list)
    flags:         dict[str, bool] = field(default_factory=dict)
    generated_at:  float = field(default_factory=time.time)
    build_time_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes":         [n.to_dict() for n in self.nodes],
            "edges":         [e.to_dict() for e in self.edges],
            "flags":         dict(self.flags),
            "generated_at":  self.generated_at,
            "build_time_ms": round(self.build_time_ms, 2),
            "node_count":    len(self.nodes),
            "edge_count":    len(self.edges),
        }


# ─────────────────────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────────────────────


class TopologyAggregator:
    """Builds a unified network graph by merging the sub-services."""

    _singleton: "TopologyAggregator | None" = None

    def __init__(self, cache_ttl: float = CACHE_TTL_SEC) -> None:
        self._cache_ttl = cache_ttl
        self._cache: Optional[TopologyGraph] = None
        self._cache_lock = asyncio.Lock()

    @classmethod
    def instance(cls) -> "TopologyAggregator":
        if cls._singleton is None:
            cls._singleton = TopologyAggregator()
        return cls._singleton

    # ── Public API ────────────────────────────────────────────

    async def build_graph(self, *, force_refresh: bool = False) -> TopologyGraph:
        """Return the cached graph or rebuild it if the TTL has elapsed."""
        now = time.time()
        if (
            not force_refresh
            and self._cache is not None
            and (now - self._cache.generated_at) < self._cache_ttl
        ):
            return self._cache

        async with self._cache_lock:
            # Re-check under the lock so a queue of waiters all share one build.
            now = time.time()
            if (
                not force_refresh
                and self._cache is not None
                and (now - self._cache.generated_at) < self._cache_ttl
            ):
                return self._cache

            started = time.perf_counter()
            graph = TopologyGraph()
            await self._collect_server_node(graph)
            await self._collect_distributed_nodes(graph)
            await self._collect_p2p_peers(graph)
            await self._collect_federation_peers(graph)
            await self._collect_overlay(graph)
            await self._collect_clients(graph)
            await self._collect_dht(graph)
            self._stitch_neighbours(graph)
            graph.build_time_ms = (time.perf_counter() - started) * 1000.0
            graph.generated_at = time.time()
            self._cache = graph
            logger.debug(
                "topology_graph_built",
                nodes=len(graph.nodes),
                edges=len(graph.edges),
                ms=graph.build_time_ms,
            )
            return graph

    # ── Collectors ────────────────────────────────────────────

    async def _collect_server_node(self, graph: TopologyGraph) -> None:
        """Always-present "self" node — the Helen Server itself."""
        try:
            hostname = socket.gethostname()
            try:
                ip = socket.gethostbyname(hostname)
            except Exception:
                ip = "127.0.0.1"

            cpu = mem = disk = 0.0
            uptime = 0.0
            try:
                import psutil
                cpu = float(psutil.cpu_percent(interval=None))
                mem = float(psutil.virtual_memory().percent)
                disk = float(psutil.disk_usage("/").percent)
                uptime = max(0.0, time.time() - psutil.boot_time())
            except Exception:
                pass

            version = ""
            try:
                from app.core.config import get_settings
                version = getattr(get_settings(), "VERSION", "") or ""
            except Exception:
                pass

            conn_count = 0
            try:
                from app.services.monitoring import get_connection_registry
                reg = get_connection_registry()
                if hasattr(reg, "count"):
                    # ``count`` is async on the real ConnectionRegistry.
                    conn_count = await reg.count()  # type: ignore[func-returns-value]
                elif hasattr(reg, "list_all"):
                    conn_count = len(reg.list_all())
            except Exception:
                conn_count = 0

            node = TopologyNode(
                id=f"server:{hostname}",
                type=NODE_TYPE_SERVER,
                layer=LAYER_PHYSICAL,
                hostname=hostname,
                ip=ip,
                version=version,
                uptime_sec=uptime,
                cpu=cpu,
                mem=mem,
                disk=disk,
                conn_count=conn_count,
                status="up",
                tags=["self"],
                metadata={
                    "platform": platform.platform(),
                    "python":   platform.python_version(),
                },
            )
            graph.nodes.append(node)
            graph.flags["server_present"] = True
        except Exception as e:  # pragma: no cover — diagnostic only
            logger.warning("topology_collect_server_failed", error=str(e))
            graph.flags["server_present"] = False

    async def _collect_distributed_nodes(self, graph: TopologyGraph) -> None:
        """Cluster-mesh nodes — every Helen-Server peer in the same cluster."""
        try:
            from app.distributed_system import node_registry as _node_reg
            nodes = _node_reg.list_all(include_dead=False)
            self_id = None
            self_node = _node_reg.self_node()
            if self_node:
                self_id = self_node.get("node_id")

            for raw in nodes:
                nid = raw.get("node_id") or ""
                if not nid:
                    continue
                # If this distributed-system node IS our own server node we
                # skip it — the local "server:<host>" node already covers it.
                if self_id and nid == self_id:
                    continue

                # Helen routers are flagged via the ``roles`` block
                # (NodeRoles dataclass → asdict'd); otherwise we treat
                # the node as a generic server.
                roles_block = raw.get("roles") or {}
                if isinstance(roles_block, dict):
                    role_tags = sorted(
                        k for k, v in roles_block.items() if v
                    )
                else:
                    role_tags = sorted(roles_block)
                if "router" in role_tags or "is_router" in role_tags:
                    ntype = NODE_TYPE_ROUTER
                else:
                    ntype = NODE_TYPE_SERVER

                load = raw.get("load") or {}
                load = load if isinstance(load, dict) else {}
                capability = raw.get("capability") or {}
                capability = capability if isinstance(capability, dict) else {}

                node = TopologyNode(
                    id=f"node:{nid}",
                    type=ntype,
                    layer=LAYER_PHYSICAL,
                    hostname=str(raw.get("host") or raw.get("hostname") or ""),
                    ip=str(raw.get("host") or raw.get("ip") or ""),
                    version=str(capability.get("version") or raw.get("version") or ""),
                    uptime_sec=float(raw.get("uptime_sec") or 0.0),
                    cpu=float(load.get("cpu_pct") or 0.0),
                    mem=float(load.get("mem_pct") or 0.0),
                    disk=float(load.get("disk_pct") or 0.0),
                    conn_count=int(load.get("active_sockets") or 0),
                    status="up" if raw.get("fresh", True) else "down",
                    tags=role_tags,
                    metadata={
                        "cluster_id": raw.get("cluster_id"),
                        "score":      raw.get("score"),
                        "strength":   raw.get("strength"),
                        "headroom":   raw.get("headroom"),
                    },
                )
                graph.nodes.append(node)

            graph.flags["distributed_disabled"] = False
        except Exception as e:
            logger.info("topology_distributed_unavailable", error=str(e))
            graph.flags["distributed_disabled"] = True

    async def _collect_p2p_peers(self, graph: TopologyGraph) -> None:
        """Local-cluster P2P peers — relays, supers, bridges, …"""
        try:
            from app.p2p.peer_federation import list_local
            from app.p2p.peer_model import PeerRole
            peers = list_local()
            for p in peers:
                if p.role is PeerRole.FEDERATION:
                    # handled in _collect_federation_peers
                    continue
                # Role → topology type mapping
                if p.role is PeerRole.RELAY:
                    ntype = NODE_TYPE_RELAY
                elif p.role is PeerRole.PROXY:
                    ntype = NODE_TYPE_RELAY
                elif p.role is PeerRole.BRIDGE:
                    ntype = NODE_TYPE_RELAY
                elif p.role is PeerRole.DISCOVERY:
                    ntype = NODE_TYPE_RENDEZVOUS
                elif p.role is PeerRole.BOOTSTRAP:
                    ntype = NODE_TYPE_RENDEZVOUS
                elif p.role is PeerRole.MONITORING:
                    ntype = NODE_TYPE_AGENT
                else:
                    ntype = NODE_TYPE_AGENT

                node = TopologyNode(
                    id=f"p2p:{p.peer_id}",
                    type=ntype,
                    layer=LAYER_PHYSICAL,
                    hostname=p.host,
                    ip=p.host,
                    status="up" if p.is_fresh() else "degraded",
                    tags=sorted(p.roles or {p.role.value}),
                    metadata={
                        "score":      p.score,
                        "cluster_id": p.cluster_id,
                        "port":       p.port,
                    },
                )
                graph.nodes.append(node)
            graph.flags["p2p_disabled"] = False
        except Exception as e:
            logger.info("topology_p2p_unavailable", error=str(e))
            graph.flags["p2p_disabled"] = True

    async def _collect_federation_peers(self, graph: TopologyGraph) -> None:
        """Cross-cluster peers — federation gateways."""
        try:
            from app.p2p.peer_federation import list_foreign
            for p in list_foreign():
                node = TopologyNode(
                    id=f"fed:{p.peer_id}",
                    type=NODE_TYPE_FEDERATION_PEER,
                    layer=LAYER_FEDERATION,
                    hostname=p.host,
                    ip=p.host,
                    status="up" if p.is_fresh() else "degraded",
                    tags=["federation", p.cluster_id],
                    metadata={
                        "cluster_id": p.cluster_id,
                        "score":      p.score,
                        "port":       p.port,
                    },
                )
                graph.nodes.append(node)
            graph.flags["federation_disabled"] = False
        except Exception as e:
            logger.info("topology_federation_unavailable", error=str(e))
            graph.flags["federation_disabled"] = True

    async def _collect_overlay(self, graph: TopologyGraph) -> None:
        """Application-layer overlay nodes + logical links."""
        try:
            from app.overlay.overlay_registry import get_overlay_registry
            from app.overlay.overlay_session import get_overlay_session_manager
            registry = get_overlay_registry()
            sessions = get_overlay_session_manager()

            for overlay_name in registry.list_names():
                g = registry.get(overlay_name)
                if g is None:
                    continue
                for n in g.all_nodes():
                    node = TopologyNode(
                        id=f"overlay:{overlay_name}:{n.node_id}",
                        type=NODE_TYPE_AGENT,
                        layer=LAYER_OVERLAY,
                        hostname=n.node_id,
                        status="up" if n.is_fresh() else "degraded",
                        tags=sorted(n.tags),
                        metadata={
                            "overlay_name": overlay_name,
                            "peer_id":      n.peer_id,
                            **dict(n.metadata or {}),
                        },
                    )
                    graph.nodes.append(node)

                for L in g.all_links():
                    link = TopologyLink(
                        src=f"overlay:{overlay_name}:{L.src_id}",
                        dst=f"overlay:{overlay_name}:{L.dst_id}",
                        transport="overlay",
                        layer=LAYER_OVERLAY,
                        rtt_ms=float(L.metadata.get("rtt_ms", 0.0))
                            if L.metadata else 0.0,
                        weight=max(0.01, 1.0 / max(0.01, L.weight)),
                        metadata={
                            "overlay_name": overlay_name,
                            "weight":       L.weight,
                            "bidirectional_hint": L.bidirectional_hint,
                        },
                    )
                    graph.edges.append(link)

            # Pin overlay-session counters onto the matching nodes.
            snap = sessions.snapshot()
            counts: dict[str, int] = {}
            for s in snap.get("sessions") or []:
                src = f"overlay:{s['overlay_name']}:{s['src_id']}"
                counts[src] = counts.get(src, 0) + 1
            for n in graph.nodes:
                if n.id in counts:
                    n.conn_count += counts[n.id]
                    n.metadata.setdefault("overlay_sessions", counts[n.id])

            graph.flags["overlay_disabled"] = False
        except Exception as e:
            logger.info("topology_overlay_unavailable", error=str(e))
            graph.flags["overlay_disabled"] = True

    async def _collect_clients(self, graph: TopologyGraph) -> None:
        """End-user clients + agents — sourced from the ConnectionRegistry."""
        try:
            from app.services.monitoring import get_connection_registry
            reg = get_connection_registry()
            connections: list = []
            if hasattr(reg, "list"):
                # ConnectionRegistry.list returns (rows, total).
                rows, _total = await reg.list(limit=10_000, offset=0)
                connections = rows or []
            elif hasattr(reg, "list_all"):
                connections = reg.list_all() or []
            elif hasattr(reg, "snapshot"):
                snap = reg.snapshot()
                if isinstance(snap, dict):
                    connections = snap.get("connections") or []

            server_id = next(
                (n.id for n in graph.nodes if n.type == NODE_TYPE_SERVER
                 and "self" in n.tags),
                None,
            )

            for c in connections:
                # ConnectionRegistry.list returns dicts with key ``id``;
                # we also accept legacy ``connection_id``/``sid`` shapes.
                def _g(name: str, default: Any = "") -> Any:
                    if isinstance(c, dict):
                        return c.get(name, default)
                    return getattr(c, name, default)

                cid = _g("id") or _g("connection_id") or _g("sid") or ""
                if not cid:
                    continue
                user_id = _g("user_id") or ""
                username = _g("username") or ""
                transport = _g("transport") or "websocket"
                ip = _g("ip") or ""
                ua = _g("user_agent") or ""
                ntype = NODE_TYPE_AGENT if "agent" in (ua or transport).lower() \
                    else NODE_TYPE_CLIENT

                node = TopologyNode(
                    id=f"client:{cid}",
                    type=ntype,
                    layer=LAYER_APPLICATION,
                    hostname=username or (ua[:64] if ua else cid[:12]),
                    ip=ip,
                    status="up",
                    tags=[user_id] if user_id else [],
                    metadata={
                        "user_id":   user_id,
                        "username":  username,
                        "transport": transport,
                        "user_agent": ua,
                    },
                )
                graph.nodes.append(node)

                # Edge: client → server. We assume the local server is the
                # one accepting this connection.
                if server_id:
                    graph.edges.append(TopologyLink(
                        src=f"client:{cid}",
                        dst=server_id,
                        transport=str(transport),
                        layer=LAYER_APPLICATION,
                        rtt_ms=float(_g("rtt_ms", 0.0) or 0.0),
                        weight=1.0,
                        metadata={"connection_id": cid},
                    ))
            graph.flags["clients_disabled"] = False
        except Exception as e:
            logger.info("topology_clients_unavailable", error=str(e))
            graph.flags["clients_disabled"] = True

    async def _collect_dht(self, graph: TopologyGraph) -> None:
        """Annotate the server node with DHT routing-table stats."""
        try:
            from app.p2p.peer_dht import dht_snapshot
            snap = dht_snapshot() or {}
            for n in graph.nodes:
                if n.type == NODE_TYPE_SERVER and "self" in n.tags:
                    n.metadata["dht"] = snap
                    break
            graph.flags["dht_disabled"] = False
        except Exception as e:
            logger.info("topology_dht_unavailable", error=str(e))
            graph.flags["dht_disabled"] = True

    # ── Edge synthesis ────────────────────────────────────────

    def _stitch_neighbours(self, graph: TopologyGraph) -> None:
        """
        Synthesise the missing transport-layer edges:
            server ↔ router  (via cluster mesh)
            server ↔ p2p peer (via host/ip)
            server ↔ federation peer

        Then populate ``node.neighbours`` for fast O(1) traversal client-side.
        """
        server_node = next(
            (n for n in graph.nodes if n.type == NODE_TYPE_SERVER
             and "self" in n.tags),
            None,
        )
        if server_node is None:
            return

        sid = server_node.id

        for other in graph.nodes:
            if other.id == sid:
                continue
            # Same physical layer — assume a mesh edge to/from the self node.
            if other.type in {NODE_TYPE_ROUTER, NODE_TYPE_SERVER}:
                graph.edges.append(TopologyLink(
                    src=sid,
                    dst=other.id,
                    transport="cluster",
                    layer=LAYER_PHYSICAL,
                    rtt_ms=float(other.metadata.get("rtt_ms", 0.0)) or 0.0,
                    weight=1.0,
                    metadata={"synthesised": True},
                ))
            elif other.type == NODE_TYPE_FEDERATION_PEER:
                graph.edges.append(TopologyLink(
                    src=sid,
                    dst=other.id,
                    transport="federation",
                    layer=LAYER_FEDERATION,
                    weight=2.0,
                    metadata={"synthesised": True},
                ))
            elif other.type == NODE_TYPE_RELAY \
                    or other.type == NODE_TYPE_RENDEZVOUS:
                graph.edges.append(TopologyLink(
                    src=sid,
                    dst=other.id,
                    transport="p2p",
                    layer=LAYER_PHYSICAL,
                    weight=1.5,
                    metadata={"synthesised": True},
                ))

        # Neighbours map.
        nbr: dict[str, set[str]] = {}
        for e in graph.edges:
            nbr.setdefault(e.src, set()).add(e.dst)
            nbr.setdefault(e.dst, set()).add(e.src)
        for n in graph.nodes:
            n.neighbours = sorted(nbr.get(n.id, set()))


def get_topology_aggregator() -> TopologyAggregator:
    """Module-level singleton accessor — preferred over direct instantiation."""
    return TopologyAggregator.instance()
