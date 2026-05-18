"""OverlayGraph — adjacency structure for one named overlay.

Each overlay name owns its own graph. Operations:

  * add/remove nodes + links
  * neighbours of a node
  * BFS shortest path
  * weight-aware k-shortest paths

Graphs are scoped to a single overlay; cross-overlay routing is
not supported here (use multiple overlays + an application-level
bridge if needed).
"""

from __future__ import annotations

import heapq
import threading
from collections import defaultdict, deque
from typing import Optional

from app.overlay.overlay_link import OverlayLink
from app.overlay.overlay_node import OverlayNode


class OverlayGraph:
    """Mutable graph for one overlay. Thread-safe."""

    def __init__(self, overlay_name: str) -> None:
        self.overlay_name = overlay_name
        self._lock = threading.RLock()
        self._nodes: dict[str, OverlayNode] = {}
        self._adj: dict[str, list[OverlayLink]] = defaultdict(list)
        self._link_idx: dict[tuple[str, str], OverlayLink] = {}

    # ── Mutation ──────────────────────────────────────────

    def add_node(self, node: OverlayNode) -> OverlayNode:
        if node.overlay_name != self.overlay_name:
            raise ValueError("overlay_name mismatch")
        with self._lock:
            existing = self._nodes.get(node.node_id)
            if existing:
                existing.last_seen = max(existing.last_seen, node.last_seen)
                existing.tags |= node.tags
                existing.metadata.update(node.metadata or {})
                if node.peer_id:
                    existing.peer_id = node.peer_id
                return existing
            self._nodes[node.node_id] = node
            return node

    def remove_node(self, node_id: str) -> bool:
        with self._lock:
            removed = self._nodes.pop(node_id, None) is not None
            self._adj.pop(node_id, None)
            for src, links in list(self._adj.items()):
                self._adj[src] = [L for L in links if L.dst_id != node_id]
            self._link_idx = {
                k: v for k, v in self._link_idx.items()
                if v.src_id != node_id and v.dst_id != node_id
            }
        return removed

    def add_link(self, link: OverlayLink) -> OverlayLink:
        if link.overlay_name != self.overlay_name:
            raise ValueError("overlay_name mismatch")
        with self._lock:
            key = (link.src_id, link.dst_id)
            existing = self._link_idx.get(key)
            if existing:
                existing.last_seen = max(existing.last_seen, link.last_seen)
                if link.weight:
                    existing.weight = link.weight
                return existing
            self._link_idx[key] = link
            self._adj[link.src_id].append(link)
            return link

    def remove_link(self, src: str, dst: str) -> bool:
        with self._lock:
            removed = self._link_idx.pop((src, dst), None) is not None
            self._adj[src] = [L for L in self._adj.get(src, [])
                              if L.dst_id != dst]
        return removed

    # ── Queries ───────────────────────────────────────────

    def node(self, node_id: str) -> Optional[OverlayNode]:
        with self._lock:
            return self._nodes.get(node_id)

    def all_nodes(self) -> list[OverlayNode]:
        with self._lock:
            return list(self._nodes.values())

    def all_links(self) -> list[OverlayLink]:
        with self._lock:
            return list(self._link_idx.values())

    def neighbours(self, node_id: str) -> list[OverlayNode]:
        with self._lock:
            seen: set[str] = set()
            out: list[OverlayNode] = []
            for L in self._adj.get(node_id, []):
                if L.dst_id in seen:
                    continue
                seen.add(L.dst_id)
                n = self._nodes.get(L.dst_id)
                if n:
                    out.append(n)
            return out

    def shortest_path(self, src: str, dst: str) -> list[str]:
        if src == dst:
            return [src]
        with self._lock:
            visited = {src}
            queue: deque[tuple[str, list[str]]] = deque([(src, [src])])
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

    def k_shortest_paths(self, src: str, dst: str,
                         k: int = 4, max_depth: int = 8) -> list[list[str]]:
        results: list[list[str]] = []
        with self._lock:
            queue: list[tuple[float, list[str]]] = [(0.0, [src])]
            heapq.heapify(queue)
            seen_paths: set[tuple[str, ...]] = set()
            while queue and len(results) < k:
                cost, path = heapq.heappop(queue)
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
                    if L.dst_id in path:  # cycle guard
                        continue
                    new_cost = cost + (1.0 / max(0.01, L.weight))
                    heapq.heappush(queue, (new_cost, path + [L.dst_id]))
        return results

    def stats(self) -> dict:
        with self._lock:
            return {
                "overlay_name": self.overlay_name,
                "node_count":   len(self._nodes),
                "link_count":   len(self._link_idx),
            }

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "overlay_name": self.overlay_name,
                "nodes": [n.to_dict() for n in self._nodes.values()],
                "links": [L.to_dict() for L in self._link_idx.values()],
            }
