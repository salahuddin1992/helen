"""Topology graph — adjacency + traversal algorithms.

The graph is the *truth source* for the topology view: it owns the
node + link + subnet collections and answers structural questions:

  * Who is directly reachable from X?
  * What's the shortest path from A to B?
  * What are the K-shortest paths (for multipath routing)?
  * Are there bridges, and which subnets do they connect?
  * What connected components exist (partition detection)?

Graph operations are pure / side-effect-free; mutation is only
through ``add_*`` / ``remove_*`` and is thread-safe via a single
RLock. Live metric updates flow via ``Link.record_success`` /
``Link.record_failure``.
"""

from __future__ import annotations

import heapq
import threading
from collections import defaultdict, deque
from typing import Iterable, Optional

from app.topology.link_model import Link, LinkType
from app.topology.node_model import Node, NodeType
from app.topology.subnet_model import Subnet


class TopologyGraph:
    """Mutable graph — singleton via ``topology_manager.get_graph``."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nodes:    dict[str, Node] = {}
        # Adjacency keyed on src — list of links. We index links by
        # (src, dst, type) so two parallel link-types can co-exist.
        self._adj:      dict[str, list[Link]] = defaultdict(list)
        self._link_idx: dict[tuple[str, str, str], Link] = {}
        self._subnets:  dict[str, Subnet] = {}     # cidr → Subnet

    # ── Mutation ──────────────────────────────────────────────

    def add_node(self, node: Node) -> Node:
        with self._lock:
            existing = self._nodes.get(node.node_id)
            if existing:
                # Merge — newer wins on liveness, union on collections.
                existing.last_seen = max(existing.last_seen, node.last_seen)
                existing.roles |= node.roles
                if node.host:
                    existing.host = node.host
                if node.port:
                    existing.port = node.port
                if node.subnet:
                    existing.subnet = node.subnet
                for nic in node.nics:
                    if nic not in existing.nics:
                        existing.nics.append(nic)
                existing.extra.update(node.extra or {})
                return existing
            self._nodes[node.node_id] = node
            if node.subnet:
                self._subnet_for(node.subnet).add_node(node.node_id)
            return node

    def remove_node(self, node_id: str) -> bool:
        with self._lock:
            n = self._nodes.pop(node_id, None)
            if n is None:
                return False
            # Drop adjacency in both directions.
            self._adj.pop(node_id, None)
            for src, links in list(self._adj.items()):
                self._adj[src] = [L for L in links if L.dst_id != node_id]
            self._link_idx = {
                k: v for k, v in self._link_idx.items()
                if v.src_id != node_id and v.dst_id != node_id
            }
            for sub in self._subnets.values():
                sub.remove_node(node_id)
            return True

    def add_link(self, link: Link) -> Link:
        with self._lock:
            existing = self._link_idx.get(link.key)
            if existing:
                existing.last_seen = max(existing.last_seen, link.last_seen)
                if link.latency_ms > 0:
                    existing.latency_ms = link.latency_ms
                if link.bandwidth_mbps > 0:
                    existing.bandwidth_mbps = link.bandwidth_mbps
                return existing
            self._link_idx[link.key] = link
            self._adj[link.src_id].append(link)
            return link

    def remove_link(self, src: str, dst: str,
                    link_type: Optional[LinkType] = None) -> int:
        """Remove all links between (src, dst), or only the given
        type if specified. Returns count removed."""
        with self._lock:
            removed = 0
            keys_to_drop = []
            for k, L in self._link_idx.items():
                if L.src_id != src or L.dst_id != dst:
                    continue
                if link_type is not None and L.link_type is not link_type:
                    continue
                keys_to_drop.append(k)
            for k in keys_to_drop:
                self._link_idx.pop(k, None)
                removed += 1
            self._adj[src] = [
                L for L in self._adj.get(src, [])
                if not (L.dst_id == dst and (
                    link_type is None or L.link_type is link_type
                ))
            ]
            return removed

    def _subnet_for(self, cidr: str) -> Subnet:
        sub = self._subnets.get(cidr)
        if sub is None:
            sub = Subnet(cidr=cidr)
            self._subnets[cidr] = sub
        return sub

    # ── Queries ───────────────────────────────────────────────

    def node(self, node_id: str) -> Optional[Node]:
        with self._lock:
            return self._nodes.get(node_id)

    def all_nodes(self) -> list[Node]:
        with self._lock:
            return list(self._nodes.values())

    def all_links(self) -> list[Link]:
        with self._lock:
            return list(self._link_idx.values())

    def all_subnets(self) -> list[Subnet]:
        with self._lock:
            return list(self._subnets.values())

    def neighbors(self, node_id: str) -> list[Node]:
        with self._lock:
            seen, out = set(), []
            for L in self._adj.get(node_id, []):
                if L.dst_id in seen:
                    continue
                seen.add(L.dst_id)
                n = self._nodes.get(L.dst_id)
                if n:
                    out.append(n)
            return out

    def edges_from(self, node_id: str) -> list[Link]:
        with self._lock:
            return list(self._adj.get(node_id, []))

    def bridges(self) -> list[Node]:
        with self._lock:
            return [n for n in self._nodes.values() if n.is_bridge()]

    # ── Path-finding ─────────────────────────────────────────

    def shortest_path(self, src: str, dst: str) -> list[str]:
        """BFS shortest path by hop count. Returns ``[]`` when no
        path exists."""
        if src == dst:
            return [src]
        with self._lock:
            visited = {src}
            queue = deque([(src, [src])])
            while queue:
                cur, path = queue.popleft()
                for L in self._adj.get(cur, []):
                    if L.dst_id in visited:
                        continue
                    if L.dst_id == dst:
                        return path + [L.dst_id]
                    visited.add(L.dst_id)
                    queue.append((L.dst_id, path + [L.dst_id]))
            return []

    def k_shortest_paths(
        self, src: str, dst: str, k: int = 4,
    ) -> list[list[str]]:
        """Yen-style K-shortest-paths by hop count (no edge weights
        for v1; weighted version can drop in by replacing the heap
        comparator)."""
        if src == dst:
            return [[src]]
        results: list[list[str]] = []
        # Pure BFS enumeration with bounded queue depth — for small K
        # this is significantly cheaper than full Yen's.
        max_depth = 8
        with self._lock:
            queue: list[tuple[int, list[str]]] = [(0, [src])]
            seen_paths: set[tuple[str, ...]] = set()
            heapq.heapify(queue)
            while queue and len(results) < k:
                length, path = heapq.heappop(queue)
                cur = path[-1]
                if cur == dst:
                    if tuple(path) in seen_paths:
                        continue
                    seen_paths.add(tuple(path))
                    results.append(path)
                    continue
                if len(path) > max_depth:
                    continue
                for L in self._adj.get(cur, []):
                    if L.dst_id in path:  # cycle prevention
                        continue
                    heapq.heappush(queue, (length + 1, path + [L.dst_id]))
        return results

    def connected_components(self) -> list[set[str]]:
        """List of connected components — useful for partition
        detection."""
        with self._lock:
            unvisited = set(self._nodes.keys())
            comps: list[set[str]] = []
            while unvisited:
                seed = unvisited.pop()
                comp = {seed}
                queue = deque([seed])
                while queue:
                    cur = queue.popleft()
                    for L in self._adj.get(cur, []):
                        if L.dst_id not in unvisited:
                            continue
                        unvisited.discard(L.dst_id)
                        comp.add(L.dst_id)
                        queue.append(L.dst_id)
                comps.append(comp)
            comps.sort(key=len, reverse=True)
            return comps

    # ── Diagnostics ──────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            by_type: dict[str, int] = defaultdict(int)
            for n in self._nodes.values():
                by_type[n.node_type.value] += 1
            link_by_type: dict[str, int] = defaultdict(int)
            for L in self._link_idx.values():
                link_by_type[L.link_type.value] += 1
            comps = self.connected_components()
            return {
                "node_count":      len(self._nodes),
                "link_count":      len(self._link_idx),
                "subnet_count":    len(self._subnets),
                "by_node_type":    dict(by_type),
                "by_link_type":    dict(link_by_type),
                "components":      len(comps),
                "largest_component": max((len(c) for c in comps), default=0),
                "bridges":         [n.node_id for n in self.bridges()],
            }

    # ── Bulk import / export ────────────────────────────────

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "nodes":   [n.to_dict() for n in self._nodes.values()],
                "links":   [L.to_dict() for L in self._link_idx.values()],
                "subnets": [s.to_dict() for s in self._subnets.values()],
            }

    def replace_from_dict(self, data: dict) -> None:
        with self._lock:
            self._nodes.clear()
            self._adj.clear()
            self._link_idx.clear()
            self._subnets.clear()
        for n_data in data.get("nodes", []):
            try:
                self.add_node(Node.from_dict(n_data))
            except Exception:
                continue
        for L_data in data.get("links", []):
            try:
                self.add_link(Link.from_dict(L_data))
            except Exception:
                continue
        for s_data in data.get("subnets", []):
            try:
                with self._lock:
                    self._subnets[s_data["cidr"]] = Subnet.from_dict(s_data)
            except Exception:
                continue
