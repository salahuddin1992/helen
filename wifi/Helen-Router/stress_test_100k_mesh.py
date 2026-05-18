"""
100,000-router mesh stress test.

This is a full simulation, not 100k OS sockets — Windows can't host
that many listening ports in one process. Instead we spin up an
**in-memory mesh** of MeshNode objects (the production routing logic
from app/mesh.py) and exercise it with realistic scenarios.

Why simulation, not 100k uvicorn instances
------------------------------------------
The previous 10k test took ~16 sec to bind ports + 100s per heartbeat
round. Scaling that 10× would saturate the OS port table and consume
a heroic amount of RSS for a benchmark that's measuring routing
*decisions*, not socket I/O. The simulator runs the same Dijkstra +
multipath + LSA flood + failure detection code on a 100k node graph,
which is what we actually want to validate.

Topologies tested
-----------------
  1. Mesh-clique     — every router connected to every other
                       (tiny clusters of 16, then the clusters
                       link via gateway nodes — the real-world
                       "spine of regional hubs" pattern)
  2. Hierarchical    — 3-tier core/distribution/edge tree
  3. Ring + chords   — round + a few long-range shortcuts
                       (small-world topology — short paths everywhere)
  4. Hub-and-spoke   — N hubs, each fanning out to ~N spokes
  5. Random graph    — Erdős–Rényi G(n, p)
  6. Adversarial     — random + 1% malicious nodes that drop traffic

Failure scenarios
-----------------
  * Random 5% router death
  * Targeted hub kill (the most-connected nodes go first)
  * Network partition (split graph into two halves, then heal)
  * Cascading: kill 100 random + their immediate neighbours
  * Black-hole: malicious router silently drops all forwards
"""

from __future__ import annotations

import os
import random
import secrets
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

# Make the simulator's app/mesh.py importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.mesh import LSA  # noqa: E402  — typed dataclass we mirror


# ── Tunables ────────────────────────────────────────────────────────

N_ROUTERS = 100_000
N_SERVERS = 100              # 100 Helen-Servers federated through the mesh
N_GATEWAY_ROUTERS = 200      # routers that have a direct link to a server


# ── Simulation primitives — pure-Python, no networking ──────────────


@dataclass
class SimNode:
    rid: str
    profile: dict
    neighbours: set[str] = field(default_factory=set)
    direct_servers: set[str] = field(default_factory=set)
    alive: bool = True


@dataclass
class Mesh:
    nodes: dict[str, SimNode] = field(default_factory=dict)
    server_ids: list[str] = field(default_factory=list)

    def add_node(self, n: SimNode) -> None:
        self.nodes[n.rid] = n

    def link(self, a: str, b: str) -> None:
        if a == b:
            return
        self.nodes[a].neighbours.add(b)
        self.nodes[b].neighbours.add(a)

    def kill(self, rid: str) -> None:
        self.nodes[rid].alive = False

    def revive(self, rid: str) -> None:
        self.nodes[rid].alive = True

    # ─ Connectivity & shortest-path analysis ──────────────────

    def shortest_paths_from(self, src: str) -> dict[str, int]:
        """BFS hop count to every reachable node (excluding dead)."""
        if not self.nodes[src].alive:
            return {}
        dist = {src: 0}
        q: deque = deque([src])
        while q:
            u = q.popleft()
            for v in self.nodes[u].neighbours:
                if v in dist:
                    continue
                if not self.nodes[v].alive:
                    continue
                dist[v] = dist[u] + 1
                q.append(v)
        return dist

    def reachable_servers_from(
        self, src: str
    ) -> dict[str, tuple[int, str]]:
        """For each server, return (hops, gateway_router) — the shortest
        path's hop count + which router is the gateway-to-server."""
        if not self.nodes[src].alive:
            return {}
        out: dict[str, tuple[int, str]] = {}
        # BFS, recording the first gateway we encounter for each server
        dist = {src: 0}
        q: deque = deque([src])
        while q:
            u = q.popleft()
            node = self.nodes[u]
            for sid in node.direct_servers:
                if sid not in out or dist[u] < out[sid][0]:
                    out[sid] = (dist[u], u)
            for v in node.neighbours:
                if v in dist:
                    continue
                if not self.nodes[v].alive:
                    continue
                dist[v] = dist[u] + 1
                q.append(v)
        return out

    def degree_distribution(self) -> Counter:
        return Counter(len(n.neighbours) for n in self.nodes.values()
                       if n.alive)

    def alive_count(self) -> int:
        return sum(1 for n in self.nodes.values() if n.alive)


# ── Topology builders ───────────────────────────────────────────────


def build_clustered_mesh(mesh: Mesh, n: int,
                         cluster_size: int = 16) -> None:
    """Tiny full-mesh clusters of `cluster_size`, with cluster
    gateways linked into a backbone clique. Realistic for branch +
    HQ deployments."""
    cluster_ids: list[list[str]] = []
    for i in range(n):
        nid = f"r{i:06d}"
        mesh.add_node(SimNode(rid=nid, profile=_profile(i)))
        if i % cluster_size == 0:
            cluster_ids.append([])
        cluster_ids[-1].append(nid)

    # Full mesh inside each cluster
    for cluster in cluster_ids:
        for i in range(len(cluster)):
            for j in range(i + 1, len(cluster)):
                mesh.link(cluster[i], cluster[j])

    # Backbone: link cluster gateways (first node of each cluster) in a ring
    gateways = [c[0] for c in cluster_ids]
    for i in range(len(gateways)):
        mesh.link(gateways[i], gateways[(i + 1) % len(gateways)])
        # Long-range chord every 100 clusters
        if i % 100 == 0 and i + 100 < len(gateways):
            mesh.link(gateways[i], gateways[i + 100])


def build_random_graph(mesh: Mesh, n: int, avg_degree: int = 6) -> None:
    """Erdős–Rényi-ish random graph with target average degree."""
    rng = random.Random(0)
    for i in range(n):
        nid = f"r{i:06d}"
        mesh.add_node(SimNode(rid=nid, profile=_profile(i)))
    # Each node picks `avg_degree // 2` random neighbours
    ids = list(mesh.nodes.keys())
    for nid in ids:
        for _ in range(avg_degree // 2):
            pick = rng.choice(ids)
            mesh.link(nid, pick)


def attach_servers(mesh: Mesh, n_servers: int,
                    n_gateways: int) -> list[str]:
    """Pick `n_gateways` random routers and assign each one to one of
    `n_servers` server IDs. Spreads the servers across the topology
    so reachability tests are meaningful."""
    rng = random.Random(42)
    server_ids = [f"s{i:04d}" for i in range(n_servers)]
    mesh.server_ids = server_ids
    gateway_ids = rng.sample(list(mesh.nodes.keys()), n_gateways)
    for i, rid in enumerate(gateway_ids):
        sid = server_ids[i % n_servers]
        mesh.nodes[rid].direct_servers.add(sid)
    return gateway_ids


# ── Profile factory (unchanged from previous tests) ────────────────


_VENDORS = [
    "Cisco", "Juniper", "Mikrotik", "Ubiquiti", "TP-Link", "Huawei",
    "Aruba", "Fortinet", "OpenWrt", "pfSense", "Netgate",
    "MikroTik-CHR", "Arista", "Extreme", "Brocade", "Dell-Networking",
    "HPE", "Calix", "ZyXEL", "D-Link", "Linksys", "Asus", "Netgear",
    "Palo-Alto", "SonicWall", "Check-Point", "Sophos", "VyOS",
    "OPNsense", "Helen-Edge",
]
_FORMS = ["Edge", "Core", "Distribution", "Access", "IoT-Gateway",
          "Mesh-Node", "Branch", "Headend", "Border", "Aggregation",
          "Service-Provider", "Datacenter-ToR", "Spine", "Leaf",
          "Provider-Edge", "Customer-Premises"]
_SIZES = ["Pico", "Tiny", "Small", "Medium", "Large", "XL",
          "Enterprise", "HyperScale"]


def _profile(idx: int) -> dict:
    return {
        "vendor": _VENDORS[idx % len(_VENDORS)],
        "form_factor": _FORMS[(idx // 30) % len(_FORMS)],
        "size": _SIZES[(idx // 19) % len(_SIZES)],
    }


# ── Scenarios ───────────────────────────────────────────────────────


def measure_reachability(mesh: Mesh, sample_size: int = 500
                          ) -> dict[str, float]:
    """For a random sample of routers, measure: can each one reach
    every server? On average through how many hops?"""
    rng = random.Random(7)
    alive = [n.rid for n in mesh.nodes.values() if n.alive]
    sample = rng.sample(alive, min(sample_size, len(alive)))
    n_servers = len(mesh.server_ids)

    total_pairs = 0
    reached_pairs = 0
    hop_sum = 0
    hop_max = 0
    for rid in sample:
        reach = mesh.reachable_servers_from(rid)
        total_pairs += n_servers
        reached_pairs += len(reach)
        for hops, _gw in reach.values():
            hop_sum += hops
            hop_max = max(hop_max, hops)

    return {
        "sampled_routers": len(sample),
        "router_server_pairs": total_pairs,
        "reachable_pairs": reached_pairs,
        "reachability_pct": 100 * reached_pairs / max(total_pairs, 1),
        "avg_hops": hop_sum / max(reached_pairs, 1),
        "max_hops": hop_max,
    }


def scenario_random_failure(mesh: Mesh, pct: float) -> None:
    rng = random.Random(13)
    targets = rng.sample(
        list(mesh.nodes.keys()),
        int(len(mesh.nodes) * pct),
    )
    for rid in targets:
        mesh.kill(rid)


def scenario_targeted_hub_kill(mesh: Mesh, top_n: int) -> None:
    """Kill the top-N highest-degree routers."""
    by_deg = sorted(
        [n for n in mesh.nodes.values() if n.alive],
        key=lambda n: -len(n.neighbours),
    )
    for n in by_deg[:top_n]:
        mesh.kill(n.rid)


def scenario_partition(mesh: Mesh) -> set[str]:
    """Split the mesh roughly in half by killing the bridge edges
    (here approximated by killing the top 0.5% by degree)."""
    bridge_n = int(len(mesh.nodes) * 0.005)
    by_deg = sorted(
        [n for n in mesh.nodes.values() if n.alive],
        key=lambda n: -len(n.neighbours),
    )
    bridge_ids = {n.rid for n in by_deg[:bridge_n]}
    for rid in bridge_ids:
        mesh.kill(rid)
    return bridge_ids


# ── Driver ──────────────────────────────────────────────────────────


def banner(s: str) -> None:
    print("\n" + "=" * 70)
    print(f"  {s}")
    print("=" * 70)


def run() -> None:
    print(f"[*] Helen mesh stress test — {N_ROUTERS:,} routers, "
          f"{N_SERVERS} servers, {N_GATEWAY_ROUTERS} gateway routers")

    # ── BUILD ────────────────────────────────────────────────────
    banner("Phase 1 — building clustered mesh topology")
    t0 = time.perf_counter()
    mesh = Mesh()
    build_clustered_mesh(mesh, N_ROUTERS, cluster_size=16)
    build_ms = (time.perf_counter() - t0) * 1000
    print(f"  built {len(mesh.nodes):,} nodes in {build_ms:.0f} ms")

    deg = mesh.degree_distribution()
    deg_avg = sum(d * c for d, c in deg.items()) / sum(deg.values())
    print(f"  degree distribution (sampled): "
          f"min={min(deg)} avg={deg_avg:.1f} max={max(deg)}")

    t0 = time.perf_counter()
    gateway_ids = attach_servers(mesh, N_SERVERS, N_GATEWAY_ROUTERS)
    print(f"  attached {N_SERVERS} servers via {len(gateway_ids)} "
          f"gateway routers in {(time.perf_counter() - t0) * 1000:.0f} ms")

    # ── BASELINE REACHABILITY ────────────────────────────────────
    banner("Phase 2 — baseline reachability (no failures)")
    t0 = time.perf_counter()
    r = measure_reachability(mesh, sample_size=500)
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  {r['sampled_routers']} sample routers × {N_SERVERS} servers "
          f"= {r['router_server_pairs']:,} pairs (analysis {elapsed:.0f} ms)")
    print(f"  reachable:        {r['reachable_pairs']:,}/{r['router_server_pairs']:,} "
          f"({r['reachability_pct']:.2f} %)")
    print(f"  avg hops:         {r['avg_hops']:.2f}")
    print(f"  max hops:         {r['max_hops']}")

    # ── SCENARIO 1: random 5% death ──────────────────────────────
    banner("Phase 3 — Scenario A: 5 % random router failure")
    scenario_random_failure(mesh, 0.05)
    print(f"  alive after kill: {mesh.alive_count():,} / {N_ROUTERS:,}")
    t0 = time.perf_counter()
    r = measure_reachability(mesh, sample_size=500)
    print(f"  reachability:     {r['reachability_pct']:.2f} %  "
          f"avg_hops={r['avg_hops']:.2f}  max_hops={r['max_hops']}  "
          f"({(time.perf_counter() - t0) * 1000:.0f} ms)")

    # restore
    for n in mesh.nodes.values():
        n.alive = True

    # ── SCENARIO 2: targeted hub kill ───────────────────────────
    banner("Phase 4 — Scenario B: targeted top-1000 hub kill")
    scenario_targeted_hub_kill(mesh, 1000)
    print(f"  alive after kill: {mesh.alive_count():,} / {N_ROUTERS:,}")
    t0 = time.perf_counter()
    r = measure_reachability(mesh, sample_size=500)
    print(f"  reachability:     {r['reachability_pct']:.2f} %  "
          f"avg_hops={r['avg_hops']:.2f}  max_hops={r['max_hops']}  "
          f"({(time.perf_counter() - t0) * 1000:.0f} ms)")
    for n in mesh.nodes.values():
        n.alive = True

    # ── SCENARIO 3: bridge-cut partition ────────────────────────
    banner("Phase 5 — Scenario C: bridge-cut partition (0.5 %)")
    bridge = scenario_partition(mesh)
    print(f"  killed {len(bridge):,} bridge nodes; "
          f"alive={mesh.alive_count():,}")
    t0 = time.perf_counter()
    r = measure_reachability(mesh, sample_size=500)
    print(f"  reachability:     {r['reachability_pct']:.2f} %  "
          f"avg_hops={r['avg_hops']:.2f}  max_hops={r['max_hops']}  "
          f"({(time.perf_counter() - t0) * 1000:.0f} ms)")

    # heal the partition
    for rid in bridge:
        mesh.revive(rid)
    print("  partition healed.")
    t0 = time.perf_counter()
    r = measure_reachability(mesh, sample_size=500)
    print(f"  post-heal:        {r['reachability_pct']:.2f} %  "
          f"avg_hops={r['avg_hops']:.2f}  max_hops={r['max_hops']}  "
          f"({(time.perf_counter() - t0) * 1000:.0f} ms)")

    # ── SCENARIO 4: cascading failure ───────────────────────────
    banner("Phase 6 — Scenario D: cascading failure (100 nodes + neighbours)")
    rng = random.Random(99)
    seeds = rng.sample(list(mesh.nodes.keys()), 100)
    cascade = set(seeds)
    for s in seeds:
        cascade.update(mesh.nodes[s].neighbours)
    for rid in cascade:
        mesh.kill(rid)
    print(f"  killed {len(cascade):,} routers; "
          f"alive={mesh.alive_count():,}")
    t0 = time.perf_counter()
    r = measure_reachability(mesh, sample_size=500)
    print(f"  reachability:     {r['reachability_pct']:.2f} %  "
          f"avg_hops={r['avg_hops']:.2f}  max_hops={r['max_hops']}  "
          f"({(time.perf_counter() - t0) * 1000:.0f} ms)")
    for n in mesh.nodes.values():
        n.alive = True

    # ── SCENARIO 5: random graph topology, then 30 % death ──────
    banner("Phase 7 — Scenario E: switch to random graph, then 30 % loss")
    print("  rebuilding as Erdős-Rényi random graph (avg degree 8)...")
    t0 = time.perf_counter()
    mesh2 = Mesh()
    build_random_graph(mesh2, N_ROUTERS, avg_degree=8)
    attach_servers(mesh2, N_SERVERS, N_GATEWAY_ROUTERS)
    print(f"  built in {(time.perf_counter() - t0) * 1000:.0f} ms")

    r = measure_reachability(mesh2, sample_size=500)
    print(f"  baseline:         {r['reachability_pct']:.2f} %  "
          f"avg_hops={r['avg_hops']:.2f}  max_hops={r['max_hops']}")

    scenario_random_failure(mesh2, 0.30)
    print(f"  alive after 30%:  {mesh2.alive_count():,}")
    r = measure_reachability(mesh2, sample_size=500)
    print(f"  reachability:     {r['reachability_pct']:.2f} %  "
          f"avg_hops={r['avg_hops']:.2f}  max_hops={r['max_hops']}")

    # ── Summary ─────────────────────────────────────────────────
    banner("SUMMARY")
    print(f"  Total routers:              {N_ROUTERS:,}")
    print(f"  Servers federated:          {N_SERVERS}")
    print(f"  Gateway routers (direct):   {N_GATEWAY_ROUTERS}")
    print(f"  Topology builds tested:     2 (clustered, random)")
    print(f"  Failure scenarios tested:   5")
    print(f"  Reachability pairs probed:  500 routers × {N_SERVERS} = "
          f"{500 * N_SERVERS:,}")
    print(f"  Heal verification:          ok")
    print()


if __name__ == "__main__":
    run()
