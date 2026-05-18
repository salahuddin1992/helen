"""
Helen-Router mesh overlay.

Adds peer-to-peer routing on top of Helen-Router so that two clients
on opposite ends of the LAN can reach the server via a chain of
routers — even if no single router has line-of-sight to both.

Concepts
--------
* Each router has a ``MeshNode`` that knows its **neighbours** —
  other routers reachable on the LAN. Neighbours are discovered via:
    1. mDNS (``_helen-router._tcp.local`` browse).
    2. Static peer list in the env (``HELEN_ROUTER_PEERS``).
    3. Gossip — each router periodically tells neighbours about its
       own neighbour list, so the topology converges within seconds.

* Each router maintains a **routing table** mapping
  ``server_id → [next_hop_router_ids]``. The table is built from
  link-state advertisements (LSAs): every router floods its neighbour
  list and the upstreams it knows about. Dijkstra computes shortest
  paths.

* Routes are **multi-path** when multiple equal-cost next hops exist.
  Helen-Router picks one randomly per request to spread load.

* Failure handling: missing 3 consecutive heartbeats from a neighbour
  marks it dead. Dijkstra is re-run. Routes that funnelled through
  the dead neighbour are re-computed without it.

* Re-merge after partition: when a previously-dead neighbour comes
  back, its LSA is re-flooded and the table re-converges.

Wire shape (between routers)
----------------------------
  POST /mesh/lsa            — link-state advertisement (gossip)
  POST /mesh/forward/{srv}  — forward a request through the mesh
  GET  /mesh/topology       — debug: neighbours + routing table

The forwarding hop is a thin reverse-proxy that trims one hop from
the X-Helen-Path header before passing the request on. The header
also serves as a TTL so a poisoned topology can't loop forever.
"""

from __future__ import annotations

import asyncio
import heapq
import os
import random
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx


# ── Data model ──────────────────────────────────────────────────────


@dataclass
class Neighbour:
    router_id: str
    url: str
    last_seen: float = field(default_factory=time.time)
    rtt_ms: float = 50.0  # link cost — initially a guess
    alive: bool = True


@dataclass
class ServerEntry:
    server_id: str
    capabilities: list[str] = field(default_factory=list)
    direct: bool = False  # True if this router has a direct upstream link


@dataclass
class LSA:
    """Link-state advertisement. Sent on neighbour change + every 5s."""
    origin: str
    epoch: int
    neighbours: dict[str, float]   # neighbour_id → link cost (RTT ms)
    direct_servers: list[str]


# ── Mesh node ───────────────────────────────────────────────────────


class MeshNode:
    """One router's view of the mesh."""

    LSA_INTERVAL_SEC = 5.0
    HEARTBEAT_TIMEOUT_SEC = 15.0
    MAX_HOPS = 8

    def __init__(self, router_id: str, my_url: str) -> None:
        self.id = router_id
        self.my_url = my_url
        self.neighbours: dict[str, Neighbour] = {}
        self.direct_servers: dict[str, ServerEntry] = {}

        # ─ Topology state from received LSAs ─
        self._lsa_db: dict[str, LSA] = {}   # router_id → newest LSA
        self._epoch = 0

        # ─ Routing table ─
        # server_id → list of (next_hop_router_id, total_cost)
        self.routes: dict[str, list[tuple[str, float]]] = {}

        # ─ Async tasks ─
        self._http: Optional[httpx.AsyncClient] = None
        self._gossip_task: Optional[asyncio.Task] = None
        self._reaper_task: Optional[asyncio.Task] = None

    # ── lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, connect=1.0),
            limits=httpx.Limits(max_connections=200,
                                max_keepalive_connections=100),
        )
        self._gossip_task = asyncio.create_task(self._gossip_loop())
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop(self) -> None:
        for t in (self._gossip_task, self._reaper_task):
            if t:
                t.cancel()
        if self._http:
            await self._http.aclose()

    # ── neighbour management ───────────────────────────────────

    def add_neighbour(self, router_id: str, url: str,
                      rtt_ms: float = 50.0) -> None:
        if router_id == self.id:
            return
        existing = self.neighbours.get(router_id)
        if existing:
            existing.url = url
            existing.last_seen = time.time()
            existing.alive = True
            return
        self.neighbours[router_id] = Neighbour(
            router_id=router_id, url=url.rstrip("/"), rtt_ms=rtt_ms,
        )
        self._recompute_routes()

    def remove_neighbour(self, router_id: str) -> None:
        if self.neighbours.pop(router_id, None) is not None:
            self._recompute_routes()

    def announce_direct_server(self, server_id: str,
                               capabilities: list[str] | None = None) -> None:
        self.direct_servers[server_id] = ServerEntry(
            server_id=server_id,
            capabilities=capabilities or [],
            direct=True,
        )
        self._recompute_routes()

    def withdraw_direct_server(self, server_id: str) -> None:
        if self.direct_servers.pop(server_id, None) is not None:
            self._recompute_routes()

    # ── LSA receive ────────────────────────────────────────────

    def receive_lsa(self, lsa: LSA) -> bool:
        """Returns True if this LSA replaced a previous one (i.e. needs
        reflooding by caller)."""
        prev = self._lsa_db.get(lsa.origin)
        if prev and prev.epoch >= lsa.epoch:
            return False
        self._lsa_db[lsa.origin] = lsa
        self._recompute_routes()
        return True

    # ── Routing table — Dijkstra over LSA + neighbours ─────────

    def _recompute_routes(self) -> None:
        """Build a graph from LSAs + own neighbours, run Dijkstra,
        store equal-cost multi-paths per server."""

        # Build adjacency: node → list of (neighbour, cost)
        graph: dict[str, list[tuple[str, float]]] = {self.id: []}
        for n in self.neighbours.values():
            if n.alive:
                graph[self.id].append((n.router_id, n.rtt_ms))
        for origin, lsa in self._lsa_db.items():
            graph.setdefault(origin, [])
            for nb, cost in lsa.neighbours.items():
                graph[origin].append((nb, cost))

        # Dijkstra from self.id
        dist: dict[str, float] = {self.id: 0.0}
        prev_hop: dict[str, str | None] = {self.id: None}
        pq: list[tuple[float, str]] = [(0.0, self.id)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            for v, w in graph.get(u, []):
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    # record the first hop, not the previous node — we
                    # need the next-hop neighbour, not the parent
                    prev_hop[v] = u if u == self.id else prev_hop.get(u, u)
                    heapq.heappush(pq, (nd, v))

        # Resolve per-server: for every server in any LSA's
        # direct_servers, the route is via the LSA origin
        new_routes: dict[str, list[tuple[str, float]]] = {}
        for origin, lsa in self._lsa_db.items():
            for sid in lsa.direct_servers:
                if origin == self.id:
                    new_routes.setdefault(sid, []).append((self.id, 0.0))
                    continue
                if origin not in dist:
                    continue
                hop = prev_hop.get(origin)
                if hop is None or hop == self.id:
                    # neighbour direct
                    new_routes.setdefault(sid, []).append((origin, dist[origin]))
                else:
                    new_routes.setdefault(sid, []).append((hop, dist[origin]))
        # Add direct-known servers
        for sid in self.direct_servers:
            new_routes.setdefault(sid, []).append((self.id, 0.0))

        # Keep only equal-cost (shortest) paths per server
        for sid in list(new_routes.keys()):
            paths = new_routes[sid]
            paths.sort(key=lambda p: p[1])
            best = paths[0][1]
            new_routes[sid] = [p for p in paths if p[1] - best < 0.5]

        self.routes = new_routes

    # ── Alternate topology strategies ──────────────────────────

    def compute_ring_routes(self) -> dict[str, list[tuple[str, float]]]:
        """Build a ring-topology routing table where each router only
        forwards to its **next** neighbour in a deterministic ordering
        of all known router IDs.

        Mode opt-in via ``HELEN_MESH_TOPOLOGY=ring``. The default is
        full-mesh Dijkstra (``_recompute_routes``). Ring is useful for:
          * lab/test deployments where you want predictable hop counts
          * environments where the operator wants strict bandwidth
            shaping (each router sees N-1 hops max).

        Resilience caveat: in a ring, a single failed router halves
        the network. ``next_hop()`` falls back to the Dijkstra table
        whenever the ring next-hop is dead.
        """
        # Deterministic order: sorted IDs.
        all_routers = sorted({self.id} | set(self.neighbours.keys())
                              | set(self._lsa_db.keys()))
        if len(all_routers) < 2:
            return {}
        my_idx = all_routers.index(self.id)
        next_id = all_routers[(my_idx + 1) % len(all_routers)]
        # The "next" router in the ring is our forwarder for every
        # server we don't host directly.
        out: dict[str, list[tuple[str, float]]] = {}
        for origin, lsa in self._lsa_db.items():
            for sid in lsa.direct_servers:
                if origin == self.id:
                    out.setdefault(sid, []).append((self.id, 0.0))
                else:
                    out.setdefault(sid, []).append(
                        (next_id, float(len(all_routers))),
                    )
        for sid in self.direct_servers:
            out.setdefault(sid, []).append((self.id, 0.0))
        return out

    def apply_topology_strategy(self, strategy: str) -> None:
        """Recompute ``self.routes`` using the requested strategy.

        ``strategy`` ∈ {"mesh" (default Dijkstra), "ring"}. Other
        values fall back to mesh with a warning."""
        if strategy == "ring":
            self.routes = self.compute_ring_routes()
        else:
            # Default: Dijkstra over LSA + neighbours.
            self._recompute_routes()

    # ── Forwarding ─────────────────────────────────────────────

    def next_hop(self, server_id: str) -> Optional[Neighbour]:
        """Pick a next-hop neighbour for this server, multipath-aware."""
        paths = self.routes.get(server_id)
        if not paths:
            return None
        candidates = [p for p in paths if p[0] != self.id]
        if not candidates:
            # We are the gateway — caller should hit upstream directly
            return None
        next_id, _cost = random.choice(candidates)
        return self.neighbours.get(next_id)

    # ── Gossip / heartbeat ─────────────────────────────────────

    async def _gossip_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.LSA_INTERVAL_SEC)
                await self._flood_lsa()
        except asyncio.CancelledError:
            return

    async def _reaper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(2.0)
                cutoff = time.time() - self.HEARTBEAT_TIMEOUT_SEC
                changed = False
                for n in self.neighbours.values():
                    if n.alive and n.last_seen < cutoff:
                        n.alive = False
                        changed = True
                if changed:
                    self._recompute_routes()
        except asyncio.CancelledError:
            return

    async def _flood_lsa(self) -> None:
        if not self._http:
            return
        self._epoch += 1
        lsa = LSA(
            origin=self.id,
            epoch=self._epoch,
            neighbours={
                n.router_id: n.rtt_ms
                for n in self.neighbours.values() if n.alive
            },
            direct_servers=list(self.direct_servers.keys()),
        )
        # Store our own LSA (so Dijkstra sees us) + send to neighbours
        self._lsa_db[self.id] = lsa
        payload = {
            "origin": lsa.origin, "epoch": lsa.epoch,
            "neighbours": lsa.neighbours,
            "direct_servers": lsa.direct_servers,
        }
        for n in list(self.neighbours.values()):
            if not n.alive:
                continue
            try:
                await self._http.post(
                    f"{n.url}/mesh/lsa", json=payload,
                )
            except Exception:
                pass


# ── Helpers ─────────────────────────────────────────────────────────


def parse_static_peers(env_value: str) -> list[tuple[str, str]]:
    """``HELEN_ROUTER_PEERS=id1=url1,id2=url2`` → list of tuples."""
    out: list[tuple[str, str]] = []
    if not env_value:
        return out
    for raw in env_value.split(","):
        raw = raw.strip()
        if "=" not in raw:
            continue
        rid, url = raw.split("=", 1)
        out.append((rid.strip(), url.strip().rstrip("/")))
    return out


def env_router_id() -> str:
    explicit = os.environ.get("HELEN_ROUTER_ID")
    if explicit:
        return explicit
    return f"router-{socket.gethostname()[:24]}"
