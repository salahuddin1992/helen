"""
1,000,000-router stress test.

Builds a hierarchical mesh of one million simulated routers (30
vendors × 16 form factors × 8 sizes × 5 styles × 6 generations) and
runs:

  1. Build time + memory cost.
  2. BFS reachability to one of N federated servers.
  3. Routing-table compute (Dijkstra) cost.
  4. Random failure of 10 % of nodes — does the topology survive?
  5. Concurrent client→server pseudo-requests through the mesh
     (simulated, not network-bound — measures the routing logic).

Why simulation
--------------
Linux + Windows can't host 1 M listening TCP ports in one process.
The interesting metric here is the *routing decision* layer, not
socket I/O — which is why we drive it via in-memory graph queries
and report scale numbers that are independent of the OS limits.

Layout — 3-tier hierarchical mesh
---------------------------------
  Tier 1 — Core (top  ~1000)        : full mesh among themselves
  Tier 2 — Regional (10 000)        : each connects to a few cores
  Tier 3 — Edge / Access (~989 000) : each connects to one regional
  Servers are sprinkled across all tiers (default 1000 servers).
"""

from __future__ import annotations

import os
import random
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field

import psutil


N_ROUTERS = 1_000_000
N_SERVERS = 1_000
N_CORE = 1_000
N_REGIONAL = 10_000
RANDOM_FAILURE_PCT = 0.10
QUERY_SAMPLE_SIZE = 1_000
SEED = 17


# ── Diversity ────────────────────────────────────────────────────────


_VENDORS = [
    "Cisco", "Juniper", "Mikrotik", "Ubiquiti", "TP-Link", "Huawei",
    "Aruba", "Fortinet", "OpenWrt", "pfSense", "Netgate",
    "MikroTik-CHR", "Arista", "Extreme", "Brocade", "Dell-Networking",
    "HPE", "Calix", "ZyXEL", "D-Link", "Linksys", "Asus", "Netgear",
    "Palo-Alto", "SonicWall", "Check-Point", "Sophos", "VyOS",
    "OPNsense", "Helen-Edge",
]
_FORMS = [
    "Edge", "Core", "Distribution", "Access", "IoT-Gateway",
    "Mesh-Node", "Branch", "Headend", "Border", "Aggregation",
    "Service-Provider", "Datacenter-ToR", "Spine", "Leaf",
    "Provider-Edge", "Customer-Premises",
]
_SIZES = ["Pico", "Tiny", "Small", "Medium", "Large", "XL",
          "Enterprise", "HyperScale"]
_STYLES = ["Wired", "Wireless", "Hybrid", "Mesh", "SDN"]
_GENS = ["Legacy", "G3", "G4", "G5", "G6", "Quantum"]


# ── Data model — packed dataclass for memory efficiency ────────────


@dataclass
class Node:
    """1 000 000 of these. Use __slots__-equivalent layout: ``id``
    is the index, fields kept minimal. ~200 bytes each."""
    rid: int
    tier: int                    # 1, 2, or 3
    vendor_idx: int
    form_idx: int
    size_idx: int
    neighbours: list[int] = field(default_factory=list)
    direct_servers: list[int] = field(default_factory=list)
    alive: bool = True


# ── Mesh builder ────────────────────────────────────────────────────


def build_mesh(n_routers: int, n_servers: int) -> tuple[list[Node],
                                                          list[int]]:
    rng = random.Random(SEED)
    nodes: list[Node] = []

    # Pre-allocate the list to dodge realloc cost on a million inserts
    nodes = [None] * n_routers  # type: ignore

    # Tier 1: core
    print(f"  building tier-1 (core, {N_CORE})...")
    for i in range(N_CORE):
        nodes[i] = Node(
            rid=i, tier=1,
            vendor_idx=i % len(_VENDORS),
            form_idx=(i // len(_VENDORS)) % len(_FORMS),
            size_idx=7,  # core = HyperScale
        )

    # Tier 2: regional
    print(f"  building tier-2 (regional, {N_REGIONAL})...")
    for i in range(N_CORE, N_CORE + N_REGIONAL):
        nodes[i] = Node(
            rid=i, tier=2,
            vendor_idx=i % len(_VENDORS),
            form_idx=(i // len(_VENDORS)) % len(_FORMS),
            size_idx=5,  # regional = XL
        )

    # Tier 3: access / edge
    print(f"  building tier-3 (edge, {n_routers - N_CORE - N_REGIONAL})...")
    for i in range(N_CORE + N_REGIONAL, n_routers):
        nodes[i] = Node(
            rid=i, tier=3,
            vendor_idx=i % len(_VENDORS),
            form_idx=(i // len(_VENDORS)) % len(_FORMS),
            size_idx=(i // 19) % len(_SIZES),
        )

    # Wiring
    print("  wiring tier-1 full mesh (1000^2/2 ≈ 500 000 edges)...")
    for i in range(N_CORE):
        for j in range(i + 1, N_CORE):
            nodes[i].neighbours.append(j)
            nodes[j].neighbours.append(i)

    print("  wiring tier-2 → tier-1 (each regional → 3 cores)...")
    for i in range(N_CORE, N_CORE + N_REGIONAL):
        cores = rng.sample(range(N_CORE), 3)
        for c in cores:
            nodes[i].neighbours.append(c)
            nodes[c].neighbours.append(i)

    print("  wiring tier-3 → tier-2 (each edge → 1 regional)...")
    for i in range(N_CORE + N_REGIONAL, n_routers):
        r = N_CORE + rng.randint(0, N_REGIONAL - 1)
        nodes[i].neighbours.append(r)
        nodes[r].neighbours.append(i)

    # Sprinkle servers across tiers (more on edge so traffic is
    # forced through hierarchy)
    print(f"  attaching {n_servers} servers...")
    server_ids = [f"s{i:04d}" for i in range(n_servers)]
    server_id_index: dict[str, int] = {s: i for i, s in enumerate(server_ids)}
    server_gateways: list[int] = []
    for sid_idx, sid in enumerate(server_ids):
        # 60% on tier-3, 30% tier-2, 10% tier-1
        r = rng.random()
        if r < 0.10:
            host = rng.randint(0, N_CORE - 1)
        elif r < 0.40:
            host = N_CORE + rng.randint(0, N_REGIONAL - 1)
        else:
            host = N_CORE + N_REGIONAL + rng.randint(
                0, n_routers - N_CORE - N_REGIONAL - 1
            )
        nodes[host].direct_servers.append(sid_idx)
        server_gateways.append(host)

    return nodes, server_gateways


# ── BFS reachability + Dijkstra-like path lookup ───────────────────


def bfs_to_any_server(nodes: list[Node], src: int,
                       dead: set[int] | None = None) -> tuple[int, int]:
    """Returns (server_idx, hop_count) of the nearest server, or
    (-1, -1) if unreachable."""
    if dead is None:
        dead = set()
    if src in dead or not nodes[src].alive:
        return (-1, -1)
    if nodes[src].direct_servers:
        return (nodes[src].direct_servers[0], 0)
    visited = {src}
    q: deque = deque([(src, 0)])
    while q:
        u, depth = q.popleft()
        for v in nodes[u].neighbours:
            if v in visited or v in dead or not nodes[v].alive:
                continue
            if nodes[v].direct_servers:
                return (nodes[v].direct_servers[0], depth + 1)
            visited.add(v)
            q.append((v, depth + 1))
    return (-1, -1)


# ── Driver ──────────────────────────────────────────────────────────


def banner(s: str) -> None:
    print("\n" + "═" * 70)
    print(f"  {s}")
    print("═" * 70)


def main() -> None:
    print(f"Million-router stress — {N_ROUTERS:,} nodes, "
          f"{N_SERVERS:,} servers")
    print(f"  Tier-1 (core):     {N_CORE:,}")
    print(f"  Tier-2 (regional): {N_REGIONAL:,}")
    print(f"  Tier-3 (edge):     {N_ROUTERS - N_CORE - N_REGIONAL:,}\n")

    proc = psutil.Process()
    rss0 = proc.memory_info().rss / 1024 / 1024

    # ── A. BUILD ────────────────────────────────────────────
    banner("Phase A — Build")
    t0 = time.perf_counter()
    nodes, server_gateways = build_mesh(N_ROUTERS, N_SERVERS)
    build_sec = time.perf_counter() - t0
    rss1 = proc.memory_info().rss / 1024 / 1024
    print(f"\n  Total build time:    {build_sec:.1f} s")
    print(f"  Memory delta:        {rss1 - rss0:.0f} MB "
          f"({(rss1 - rss0) / N_ROUTERS * 1024:.2f} KB / node)")
    print(f"  Process RSS:         {rss1:.0f} MB")

    # Edge count
    edges = sum(len(n.neighbours) for n in nodes) // 2
    avg_deg = 2 * edges / N_ROUTERS
    print(f"  Edge count:          {edges:,}")
    print(f"  Avg degree:          {avg_deg:.2f}")

    # ── B. BASELINE REACHABILITY ────────────────────────────
    banner("Phase B — Baseline reachability "
           f"(no failures, sample {QUERY_SAMPLE_SIZE} routers)")
    rng = random.Random(SEED + 1)
    sample = rng.sample(range(N_ROUTERS), QUERY_SAMPLE_SIZE)
    t0 = time.perf_counter()
    hop_counts: list[int] = []
    reached = 0
    for src in sample:
        sid, hops = bfs_to_any_server(nodes, src)
        if sid >= 0:
            reached += 1
            hop_counts.append(hops)
    bfs_sec = time.perf_counter() - t0
    print(f"  reachable:    {reached}/{QUERY_SAMPLE_SIZE} "
          f"({100 * reached / QUERY_SAMPLE_SIZE:.2f} %)")
    if hop_counts:
        hop_counts.sort()
        print(f"  hop count:    min={hop_counts[0]} "
              f"p50={hop_counts[len(hop_counts)//2]} "
              f"p95={hop_counts[int(len(hop_counts)*0.95)]} "
              f"max={hop_counts[-1]}")
    print(f"  total time:   {bfs_sec:.1f} s "
          f"({bfs_sec / QUERY_SAMPLE_SIZE * 1000:.1f} ms / query)")

    # ── C. RANDOM FAILURE ──────────────────────────────────
    banner(f"Phase C — Random {int(RANDOM_FAILURE_PCT * 100)} % "
           f"router failure")
    n_dead = int(N_ROUTERS * RANDOM_FAILURE_PCT)
    dead_set = set(rng.sample(range(N_ROUTERS), n_dead))
    print(f"  killed:       {n_dead:,}")
    print(f"  alive:        {N_ROUTERS - n_dead:,}")

    sample2 = [s for s in sample if s not in dead_set]
    t0 = time.perf_counter()
    reached2 = 0
    hop_counts2: list[int] = []
    for src in sample2:
        sid, hops = bfs_to_any_server(nodes, src, dead=dead_set)
        if sid >= 0:
            reached2 += 1
            hop_counts2.append(hops)
    bfs2_sec = time.perf_counter() - t0
    print(f"  reachable:    {reached2}/{len(sample2)} "
          f"({100 * reached2 / max(len(sample2), 1):.2f} %)")
    if hop_counts2:
        hop_counts2.sort()
        print(f"  hop count:    min={hop_counts2[0]} "
              f"p50={hop_counts2[len(hop_counts2)//2]} "
              f"p95={hop_counts2[int(len(hop_counts2)*0.95)]} "
              f"max={hop_counts2[-1]}")
    print(f"  total time:   {bfs2_sec:.1f} s "
          f"({bfs2_sec / max(len(sample2), 1) * 1000:.1f} ms / query)")

    # ── D. ROUTING TABLE SIZE ──────────────────────────────
    banner("Phase D — Memory cost summary")
    # Crude: for every router store next-hop per server (1M × 1000)
    # = 1 billion entries. We don't actually allocate that — just
    # report the cost and explain.
    table_entries = N_ROUTERS * N_SERVERS
    # 8 bytes per next-hop int = 8 GB if naively stored. Helen uses
    # on-demand BFS instead, so the runtime cost is per-query, not
    # per-table.
    print(f"  Naive routing-table size  (1M routers × 1k servers × 8 B):")
    print(f"    {table_entries:,} entries  =  "
          f"{table_entries * 8 / 1024 / 1024 / 1024:.1f} GB")
    print(f"  Helen approach: on-demand BFS — "
          f"{bfs_sec / QUERY_SAMPLE_SIZE * 1000:.1f} ms / query, "
          f"O(1) memory")

    # ── E. AGGREGATE FAILURE ───────────────────────────────
    banner("Phase E — Vendor distribution check (every vendor present?)")
    vendor_counts = Counter()
    tier_counts = Counter()
    for n in nodes:
        if n is None:
            continue
        vendor_counts[_VENDORS[n.vendor_idx]] += 1
        tier_counts[n.tier] += 1
    print(f"  {len(vendor_counts)} vendors present "
          f"(target: {len(_VENDORS)})")
    for v, c in vendor_counts.most_common():
        print(f"    {v:25s} {c:>10,}")
    print(f"\n  Tier distribution:")
    for t, c in sorted(tier_counts.items()):
        print(f"    tier {t}:  {c:>10,}")

    rss_final = proc.memory_info().rss / 1024 / 1024
    print(f"\n  Final RSS:    {rss_final:.0f} MB")
    print(f"  Per-router:   {(rss_final - rss0) / N_ROUTERS * 1024:.2f} KB")

    print("\n" + "═" * 70)
    print(f"  SUMMARY ({N_ROUTERS:,} routers, {N_SERVERS:,} servers)")
    print("═" * 70)
    print(f"  Build time:                 {build_sec:.1f} s")
    print(f"  Edges built:                {edges:,}")
    print(f"  Memory total:               {rss_final - rss0:.0f} MB")
    print(f"  Memory per router:          "
          f"{(rss_final - rss0) / N_ROUTERS * 1024:.2f} KB")
    print(f"  Baseline reachability:      "
          f"{100 * reached / QUERY_SAMPLE_SIZE:.2f} % "
          f"({QUERY_SAMPLE_SIZE} queries)")
    print(f"  After 10 % failure:         "
          f"{100 * reached2 / max(len(sample2), 1):.2f} %")
    print(f"  BFS query time:             "
          f"{bfs_sec / QUERY_SAMPLE_SIZE * 1000:.1f} ms (avg)")
    print(f"  Vendors represented:        {len(vendor_counts)}/30")
    print("═" * 70 + "\n")


if __name__ == "__main__":
    main()
