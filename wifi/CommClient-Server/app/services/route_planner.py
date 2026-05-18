"""
Route planner — compute the path an envelope should take from source
to destination.

Two modes
---------
1. **production** (default) — shortest path with load-weighted edges.
   Result has 0–3 hops on a healthy multi-region cluster. This is
   what runs in production. Edge weight = ``base_latency × (1 +
   load_penalty(neighbor))`` where ``load_penalty`` is derived from
   ``health_score``.

2. **chaos_chain** (``HELEN_ENABLE_100_HOP_TEST_MODE=true``) — builds
   a deterministic 100-hop chain through every healthy server in the
   registry, repeating round-robin if fewer than 100 servers exist.
   **This is for control-plane stress testing only.** The
   ``RouteExecutor`` enforces ``Envelope.size_bytes() <= 8KB`` and
   refuses ``plane='data'`` events on chain mode, so media/files
   never traverse the chain.

Why not put this in topology_manager?
-------------------------------------
``topology_manager`` is per-call (mesh ↔ SFU decisions). The route
planner operates at the cross-server fabric layer, independent of
any specific call. They communicate only indirectly: topology decides
where the SFU lives; route_planner finds a path to that SFU.

API
---
    >>> planner = RoutePlanner(registry_service, this_server_id="server_001")
    >>> route = await planner.plan(source="server_001", dest="server_037")
    >>> # → ["server_001", "server_037"]      (1-hop direct in healthy cluster)
    >>> route = await planner.plan(..., mode="chaos_chain")
    >>> # → ["server_001", "server_002", ..., "server_100", "server_037"]
"""

from __future__ import annotations

import heapq
import os
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

# Default base-latency between any two unmeasured servers, in
# arbitrary units (smaller = faster). Production deployments can
# override via ``RouteEdgeProvider`` (not yet wired — see Phase 5).
DEFAULT_BASE_LATENCY = 1.0

# How aggressively load impacts edge weight. With penalty=5 and
# health_score=0, an unhealthy node costs 6× a healthy one.
LOAD_PENALTY_MULTIPLIER = 5.0

# Chaos chain target length when the operator opts in. Capped at the
# envelope max_hops ceiling (128) inside Envelope validation.
CHAOS_CHAIN_DEFAULT = 100


def _is_chaos_enabled() -> bool:
    raw = os.environ.get("HELEN_ENABLE_100_HOP_TEST_MODE", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


class RoutePlanner:
    def __init__(
        self,
        registry_service,
        this_server_id: str = "local",
        chaos_chain_length: int = CHAOS_CHAIN_DEFAULT,
    ) -> None:
        self._registry = registry_service
        self._sid = this_server_id
        self._chaos_len = chaos_chain_length

    async def plan(
        self,
        source: str,
        dest: str,
        mode: str = "production",
        trace_id: Optional[str] = None,
    ) -> list[str]:
        """Return an ordered list of server_ids for the route. The
        list always starts with ``source`` and ends with ``dest``.
        Length 2 means direct delivery.

        Raises ``RuntimeError`` if mode is "chaos_chain" but the
        operator hasn't opted in via env flag — refuses to build a
        100-hop route in production.

        Chaos hook: if the chaos admin endpoint has registered a
        forced route for ``trace_id``, we honor it (consume-on-read
        — the override fires exactly once). Lets a test verify a
        specific deterministic chain end-to-end.
        """
        if trace_id is not None:
            try:
                from app.api.routes.chaos import get_forced_route as _gfr
                forced = _gfr(trace_id)
                if forced is not None:
                    return forced
            except Exception:
                pass

        if mode == "chaos_chain":
            if not _is_chaos_enabled():
                raise RuntimeError(
                    "chaos_chain mode requires HELEN_ENABLE_100_HOP_TEST_MODE=true. "
                    "Refusing to build deterministic 100-hop route in production."
                )
            return await self._build_chaos_chain(source, dest)

        if mode == "production":
            return await self._shortest_path(source, dest)

        raise ValueError(f"unknown route mode: {mode!r}")

    # ── Production: shortest path ──────────────────────────────

    async def _shortest_path(self, source: str, dest: str) -> list[str]:
        """Dijkstra over the registry graph with load-weighted edges.

        For now the topology is fully connected — every healthy
        server is a potential next hop from every other. In
        practice this means the optimal route is almost always
        ``[source, dest]`` (direct), unless ``dest`` is unhealthy
        and we need to relay via a healthy intermediary that has a
        better path to it. We still go through Dijkstra so that:

          (a) once we add region/AZ-aware base latencies, the
              algorithm transparently picks the right path;
          (b) load weighting can route around an overloaded node
              that's directly reachable but expensive.
        """
        if source == dest:
            return [source]

        servers = await self._registry.list_all_healthy()
        sids = {s.server_id for s in servers}
        # Always include source and dest even if registry hasn't
        # heard from them recently — caller assumed they exist.
        sids.add(source)
        sids.add(dest)
        if dest not in sids:
            # Caller asked us to route to a server we don't know.
            # Direct is the only option.
            return [source, dest]

        loads = await self._registry.all_loads()

        # Edge weight: 1 + load_penalty(neighbor). Source has no
        # incoming edge to weight on, so we ignore self-load.
        def edge_weight(_u: str, v: str) -> float:
            base = DEFAULT_BASE_LATENCY
            v_load = loads.get(v)
            if v_load is None:
                # Unknown load = treat as healthy.
                return base
            score = max(0.0, min(1.0, v_load.health_score))
            penalty = (1.0 - score) * LOAD_PENALTY_MULTIPLIER
            return base * (1.0 + penalty)

        # Dijkstra. With a fully-connected graph and uniform base
        # latencies, this degenerates to "direct edge if dest is
        # healthy, otherwise pick lowest-weight intermediary".
        dist: dict[str, float] = {source: 0.0}
        prev: dict[str, str] = {}
        pq: list[tuple[float, str]] = [(0.0, source)]
        seen: set[str] = set()

        while pq:
            d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u == dest:
                break
            for v in sids:
                if v == u or v in seen:
                    continue
                w = edge_weight(u, v)
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))

        if dest not in dist:
            # Disconnected — fall back to direct.
            return [source, dest]

        # Reconstruct path.
        path = [dest]
        cur = dest
        while cur in prev:
            cur = prev[cur]
            path.append(cur)
        path.reverse()
        return path

    # ── Chaos chain ────────────────────────────────────────────

    async def _build_chaos_chain(self, source: str, dest: str) -> list[str]:
        """Construct a deterministic chain of length ``_chaos_len``.

        Excludes ``source`` and ``dest`` from the chain body — they
        bookend it. Servers are chosen in a stable order (by
        server_id sort) so a given (source, dest) pair always
        produces the same chain across runs (until the registry
        membership changes).
        """
        servers = await self._registry.list_all_healthy()
        body_pool = sorted(
            s.server_id for s in servers
            if s.server_id != source and s.server_id != dest
        )
        if not body_pool:
            # Only source and dest exist — degenerate to direct.
            return [source, dest]

        target_body = max(0, self._chaos_len - 2)  # subtract source+dest
        if target_body == 0:
            return [source, dest]

        if len(body_pool) >= target_body:
            body = body_pool[:target_body]
        else:
            # Round-robin reuse so we still produce a long chain even
            # in a small cluster. This is the lab-test mode anyway.
            body = []
            i = 0
            while len(body) < target_body:
                body.append(body_pool[i % len(body_pool)])
                i += 1

        return [source] + body + [dest]


# ── Module-level singleton ──────────────────────────────────────────

_svc: Optional[RoutePlanner] = None


def get_route_planner() -> RoutePlanner:
    global _svc
    if _svc is None:
        from app.services.server_registry_service import get_registry_service
        _svc = RoutePlanner(
            registry_service=get_registry_service(),
            this_server_id="local",
        )
    return _svc


def configure(*, registry_service, this_server_id: str,
              chaos_chain_length: int = CHAOS_CHAIN_DEFAULT) -> RoutePlanner:
    global _svc
    _svc = RoutePlanner(
        registry_service=registry_service,
        this_server_id=this_server_id,
        chaos_chain_length=chaos_chain_length,
    )
    logger.info(
        "route_planner_configured",
        server_id=this_server_id,
        chaos_chain_length=chaos_chain_length,
        chaos_enabled=_is_chaos_enabled(),
    )
    return _svc
