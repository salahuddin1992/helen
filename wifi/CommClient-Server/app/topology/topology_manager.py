"""Topology Manager — singleton coordinator + background sync loop.

The manager is the public entry point of the ``app.topology``
package. It:

  1. Owns the singleton ``TopologyGraph``.
  2. Pulls live state from ``app.services.node_registry`` +
     ``app.services.peer_registry`` + ``app.services.path_health``
     and reflects it into the graph.
  3. Persists the graph to disk every cycle via ``TopologyStore``.
  4. Exposes high-level queries (snapshot, neighbors, paths, ...)
     for the API + UI.

The pull-from-services-into-graph approach (rather than the inverse)
keeps the topology package additive: nothing in services/ has to
know the topology package exists.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

from app.topology.bridge_model import Bridge
from app.topology.link_model import Link, LinkType
from app.topology.node_model import Node, NodeType
from app.topology.subnet_model import (
    Subnet,
    infer_subnet,
    list_local_subnets,
)
from app.topology.topology_graph import TopologyGraph
from app.topology.topology_store import get_topology_store


REFRESH_INTERVAL_SEC = 30.0


class TopologyManager:
    _singleton: "TopologyManager | None" = None

    def __init__(self) -> None:
        self._graph = TopologyGraph()
        self._loaded_at: float = 0.0
        self._last_sync_at: float = 0.0
        self._cycle_count: int = 0
        self._loop_task: Optional[asyncio.Task] = None
        self._running: bool = False
        # Restore previous graph if we have one.
        self._restore_from_disk()

    @classmethod
    def instance(cls) -> "TopologyManager":
        if cls._singleton is None:
            cls._singleton = TopologyManager()
        return cls._singleton

    # ── Public API ────────────────────────────────────────────

    @property
    def graph(self) -> TopologyGraph:
        return self._graph

    def snapshot(self) -> dict:
        return {
            "stats":          self._graph.stats(),
            "loaded_at":      self._loaded_at,
            "last_sync_at":   self._last_sync_at,
            "cycle_count":    self._cycle_count,
            "store":          get_topology_store().info(),
            "graph":          self._graph.to_dict(),
        }

    def neighbors(self, node_id: str) -> list[dict]:
        return [n.to_dict() for n in self._graph.neighbors(node_id)]

    def paths(self, src: str, dst: str, k: int = 4) -> list[list[str]]:
        return self._graph.k_shortest_paths(src, dst, k=k)

    def partitions(self) -> list[list[str]]:
        return [sorted(c) for c in self._graph.connected_components()]

    def bridges(self) -> list[dict]:
        return [n.to_dict() for n in self._graph.bridges()]

    def subnets(self) -> list[dict]:
        return [s.to_dict() for s in self._graph.all_subnets()]

    # ── Persistence ──────────────────────────────────────────

    def _restore_from_disk(self) -> None:
        data = get_topology_store().load()
        if not data:
            return
        self._graph.replace_from_dict(data)
        self._loaded_at = data.get("saved_at", time.time())
        logger.info(
            "topology_loaded_from_disk",
            saved_at=self._loaded_at,
            stats=self._graph.stats(),
        )

    def _persist_to_disk(self) -> None:
        get_topology_store().save(self._graph.to_dict())

    # ── Sync from services ───────────────────────────────────

    def sync_once(self) -> dict:
        """One pull cycle from the live service layer into the graph."""
        absorbed_nodes = self._absorb_nodes()
        absorbed_links = self._absorb_links_from_path_health()
        self._persist_to_disk()
        self._last_sync_at = time.time()
        self._cycle_count += 1
        return {
            "absorbed_nodes": absorbed_nodes,
            "absorbed_links": absorbed_links,
            "stats":          self._graph.stats(),
        }

    def _absorb_nodes(self) -> int:
        """Pull every fresh peer from node_registry into the graph.

        Self gets a NodeType.PEER (or BRIDGE if multi-NIC). Peers come
        in as PEER unless they advertise bridge=True, in which case
        they're promoted to BRIDGE.
        """
        absorbed = 0
        try:
            from app.services.node_registry import get_registry
        except ImportError:
            return absorbed

        local_subnets = list_local_subnets()
        reg = get_registry()
        for n in reg.nodes(include_dead=True):
            host = n.host or ""
            subnet = infer_subnet(host)
            roles = set()
            try:
                for fld in (
                    "signaling", "messaging", "presence", "sfu",
                    "relay", "recording", "file_transfer", "metrics",
                ):
                    if getattr(n.roles, fld, False):
                        roles.add(fld)
            except Exception:
                pass

            extra = dict(getattr(n, "extra", None) or {})
            is_bridge_extra = bool(extra.get("bridge")) or len(local_subnets) >= 2
            if is_bridge_extra and n.self_node:
                node = Bridge(
                    node_id=n.node_id,
                    node_type=NodeType.BRIDGE,
                    host=host,
                    port=n.port,
                    subnet=subnet,
                    nics=list(local_subnets),
                    cluster_id=getattr(n, "cluster_id", "default") or "default",
                    roles=roles,
                    capabilities={
                        "cpu_cores": n.capability.cpu_cores,
                        "ram_gb":    n.capability.ram_gb,
                        "nic_gbps":  n.capability.nic_gbps,
                    },
                    is_self=n.self_node,
                    last_seen=n.last_heartbeat,
                    extra=extra,
                    subnets=list(local_subnets),
                    host_aliases=list(extra.get("host_aliases", [])) or [host],
                    forwarding=True,
                )
            else:
                node = Node(
                    node_id=n.node_id,
                    node_type=NodeType.PEER,
                    host=host,
                    port=n.port,
                    subnet=subnet,
                    nics=[host] if host else [],
                    cluster_id=getattr(n, "cluster_id", "default") or "default",
                    roles=roles,
                    capabilities={
                        "cpu_cores": n.capability.cpu_cores,
                        "ram_gb":    n.capability.ram_gb,
                        "nic_gbps":  n.capability.nic_gbps,
                    },
                    is_self=n.self_node,
                    last_seen=n.last_heartbeat,
                    extra=extra,
                )
            self._graph.add_node(node)
            absorbed += 1

        # Also absorb bridge metadata from peer_registry (host_aliases).
        try:
            from app.services.peer_registry import peer_registry
            # peer_registry.get is async — we run it sync via asyncio if a
            # loop is around; otherwise skip.
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._merge_peer_aliases())
            except RuntimeError:
                pass
        except Exception:
            pass

        return absorbed

    async def _merge_peer_aliases(self) -> None:
        """Async helper — pulls host_aliases from peer_registry into
        existing graph nodes (promoting them to Bridge when relevant)."""
        try:
            from app.services.peer_registry import peer_registry
        except ImportError:
            return
        for n in self._graph.all_nodes():
            try:
                meta = await peer_registry.get(n.node_id)
            except Exception:
                continue
            if not meta:
                continue
            aliases = list(getattr(meta, "host_aliases", None) or [])
            if aliases:
                # If aliases imply > 1 subnet, promote node to Bridge.
                subs = {infer_subnet(a) for a in aliases if a}
                subs.discard(None)
                if len(subs) >= 2 and n.node_type is NodeType.PEER:
                    n.node_type = NodeType.BRIDGE
                    n.extra["bridge"] = True
                n.extra.setdefault("host_aliases", aliases)

    def _absorb_links_from_path_health(self) -> int:
        """Translate path_health (host:port → latency) into LAN links
        between this node and every peer it has measured."""
        absorbed = 0
        try:
            from app.services.path_health import get_path_health
        except ImportError:
            return absorbed

        snap = get_path_health().snapshot()
        # Find self.
        me = next(
            (n for n in self._graph.all_nodes() if n.is_self),
            None,
        )
        if me is None:
            return absorbed

        # Map (host:port) → node_id from current graph.
        addr_to_id: dict[tuple[str, int], str] = {}
        for n in self._graph.all_nodes():
            if n.host and n.port:
                addr_to_id[(n.host, n.port)] = n.node_id

        for p in snap.get("paths", []):
            try:
                host, port_s = p["key"].rsplit(":", 1)
                port = int(port_s)
            except Exception:
                continue
            dst_id = addr_to_id.get((host, port))
            if not dst_id or dst_id == me.node_id:
                continue
            link_type = LinkType.LAN_DIRECT
            # Cross-subnet → BRIDGE class.
            dst = self._graph.node(dst_id)
            if dst and dst.subnet and me.subnet and dst.subnet != me.subnet:
                link_type = LinkType.BRIDGE
            link = Link(
                src_id=me.node_id,
                dst_id=dst_id,
                link_type=link_type,
                latency_ms=float(p.get("latency_ms") or 0.0),
                last_seen=time.time(),
                last_success=(
                    time.time()
                    if p.get("last_success_age_s") is not None
                    else 0.0
                ),
                fail_count=int(p.get("fail_count") or 0),
            )
            self._graph.add_link(link)
            absorbed += 1
        return absorbed

    # ── Background loop ──────────────────────────────────────

    async def _run_loop(self) -> None:
        self._running = True
        logger.info(
            "topology_manager_started",
            interval_sec=REFRESH_INTERVAL_SEC,
        )
        try:
            while self._running:
                try:
                    self.sync_once()
                except Exception as e:
                    logger.warning("topology_manager_cycle_failed", error=str(e))
                await asyncio.sleep(REFRESH_INTERVAL_SEC)
        finally:
            logger.info("topology_manager_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="topology-manager",
            )
        except RuntimeError:
            logger.warning("topology_manager_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None


def get_topology_manager() -> TopologyManager:
    return TopologyManager.instance()


def start_topology_manager() -> None:
    get_topology_manager().start()


def stop_topology_manager() -> None:
    get_topology_manager().stop()
