"""
Multi-path router — adaptive route selection across the mesh.

This module is the *orchestrator* that ties together every routing
primitive built in the rest of ``app/services/``:

  * ``path_health``        — passive latency tracking
  * ``latency_prober``     — active RTT samples
  * ``load_balancer``      — weighted proxy ranking
  * ``trust_score``        — peer reputation
  * ``adaptive_timeout``   — RFC 6298 RTO per peer
  * ``phi_accrual``        — failure suspicion
  * ``backpressure``       — overload signal
  * ``partition_detector`` — quorum awareness
  * ``cluster_mesh``       — recursive relay chain
  * ``peer_registry``      — host_aliases / bridge flags
  * ``consistent_hash``    — replica selection

Goal
----
Given a (target_node_id, request) pair, pick the best **K** routes
from up to 10 route classes, score them, race the top one against an
adaptive deadline, and fail over instantly to the next-best route on
timeout — without the caller knowing which class was picked.

Route classes (auto-elected by ``select_strategy`` based on live
conditions: LAN reachability, NAT type, partition state, load):

    DIRECT            — http://target.host:port/...
    LAN_ALIAS         — every host_aliases entry on a multi-NIC peer
    BRIDGE            — proxy via a peer with bridge=True (cross-subnet)
    SINGLE_HOP_RELAY  — proxy via best-scored neighbour
    MULTI_HOP_RELAY   — recursive 2..4-hop chain
    REVERSE_TUNNEL    — outbound WS via Helen-Rendezvous
    HOLE_PUNCH        — UDP NAT traversal (skeleton)
    FEDERATION        — cross-cluster HMAC tunnel
    CACHED_FALLBACK   — last-known-good host from peers_cache
    RENDEZVOUS_HINT   — last route advertised by Helen-Rendezvous

The router maintains a per-target *route table* with rolling metrics
(latency EWMA, failure count, last_used, last_success, score). It
self-repairs: failed routes go into a 30 s cooldown, and a periodic
loop re-probes them so a transient outage doesn't permanently demote
a path that's actually fine.

Cooperative with backpressure
-----------------------------
When the local backpressure gate is REJECTED, we don't even try
DIRECT — the local CPU is already saturated, and the new request
would just queue and time out. We jump straight to peer-relay routes
so the work actually leaves the box.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Route classes & data model ─────────────────────────────────


class RouteType(str, Enum):
    DIRECT            = "direct"
    LAN_ALIAS         = "lan_alias"
    BRIDGE            = "bridge"
    SINGLE_HOP_RELAY  = "single_hop_relay"
    MULTI_HOP_RELAY   = "multi_hop_relay"
    REVERSE_TUNNEL    = "reverse_tunnel"
    HOLE_PUNCH        = "hole_punch"
    FEDERATION        = "federation"
    CACHED_FALLBACK   = "cached_fallback"
    RENDEZVOUS_HINT   = "rendezvous_hint"


# Default weights — sum is normalised at score time so individual
# tweaks don't break ordering.
ROUTE_WEIGHTS = {
    "latency":  0.25,
    "loss":     0.15,
    "bw":       0.10,
    "trust":    0.15,
    "load":     0.10,
    "hops":     0.10,
    "age":      0.05,
    "security": 0.05,
    "nat":      0.05,
}

# Hard rejection thresholds.
PHI_REJECT_THRESHOLD = 8.0       # peer suspected dead
TRUST_REJECT_THRESHOLD = 0.10    # quarantined
COOLDOWN_AFTER_FAIL_SEC = 30.0
REFRESH_INTERVAL_SEC = 30.0


@dataclass
class Route:
    target_node_id: str
    route_type:     RouteType
    hops:           list[str] = field(default_factory=list)  # node_ids in order
    first_host:     str = ""
    first_port:     int = 0
    score:          float = 0.0
    last_score_at:  float = 0.0
    last_used_at:   float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    consecutive_failures: int = 0
    failed_until:   float = 0.0  # cooldown deadline

    @property
    def key(self) -> str:
        return f"{self.target_node_id}|{self.route_type.value}|{'/'.join(self.hops)}"

    @property
    def hop_count(self) -> int:
        return len(self.hops)

    def is_in_cooldown(self, now: Optional[float] = None) -> bool:
        n = now if now is not None else time.time()
        return n < self.failed_until


# ── Route table ────────────────────────────────────────────────


class RouteTable:
    _singleton: "RouteTable | None" = None

    def __init__(self) -> None:
        self._routes: dict[str, Route] = {}

    @classmethod
    def instance(cls) -> "RouteTable":
        if cls._singleton is None:
            cls._singleton = RouteTable()
        return cls._singleton

    def upsert(self, r: Route) -> Route:
        existing = self._routes.get(r.key)
        if existing:
            return existing
        self._routes[r.key] = r
        return r

    def for_target(self, target_id: str) -> list[Route]:
        return [r for r in self._routes.values() if r.target_node_id == target_id]

    def all(self) -> list[Route]:
        return list(self._routes.values())

    def evict_stale(self, max_age_sec: float = 600.0) -> int:
        cutoff = time.time() - max_age_sec
        dead = [
            k for k, r in self._routes.items()
            if max(r.last_used_at, r.last_success_at) < cutoff
        ]
        for k in dead:
            self._routes.pop(k, None)
        return len(dead)


def get_route_table() -> RouteTable:
    return RouteTable.instance()


# ── Discovery — build candidate routes for a target ─────────────


async def discover_routes(target_node_id: str) -> list[Route]:
    """Materialise every routable path to ``target_node_id`` from
    the current cluster state. Cheap (no network) — this is the
    *enumeration* step; ``score_route`` then ranks them."""
    out: list[Route] = []
    try:
        from app.services.node_registry import get_registry
        from app.services.peer_registry import peer_registry
    except ImportError:
        return out

    reg = get_registry()
    target = next(
        (n for n in reg.nodes(include_dead=True) if n.node_id == target_node_id),
        None,
    )
    if target is None:
        return out

    table = get_route_table()

    # 1. DIRECT — primary host.
    out.append(table.upsert(Route(
        target_node_id=target_node_id,
        route_type=RouteType.DIRECT,
        hops=[target_node_id],
        first_host=target.host,
        first_port=target.port,
    )))

    # 2. LAN_ALIAS — every other interface on a multi-NIC peer.
    try:
        meta = await peer_registry.get(target_node_id)
        for alias in (getattr(meta, "host_aliases", None) or []):
            if alias and alias != target.host:
                out.append(table.upsert(Route(
                    target_node_id=target_node_id,
                    route_type=RouteType.LAN_ALIAS,
                    hops=[target_node_id],
                    first_host=alias,
                    first_port=target.port,
                )))
    except Exception:
        pass

    # 3. BRIDGE — peers advertising bridge=True act as cross-subnet
    #    proxies; high priority for LAN-segmented topologies.
    bridges = [
        n for n in reg.nodes(include_dead=False)
        if not n.self_node and n.node_id != target_node_id
        and bool((n.extra or {}).get("bridge", False))
    ]
    for b in bridges[:8]:
        out.append(table.upsert(Route(
            target_node_id=target_node_id,
            route_type=RouteType.BRIDGE,
            hops=[b.node_id, target_node_id],
            first_host=b.host,
            first_port=b.port,
        )))

    # 4. SINGLE_HOP_RELAY — top-scored proxy candidates.
    try:
        from app.services.load_balancer import rank_proxies
        candidates = [
            n for n in reg.nodes(include_dead=False)
            if not n.self_node and n.node_id != target_node_id
        ]
        for s in rank_proxies(candidates, top_k=8):
            if s.node.node_id in {b.node_id for b in bridges[:8]}:
                continue  # already added as BRIDGE
            out.append(table.upsert(Route(
                target_node_id=target_node_id,
                route_type=RouteType.SINGLE_HOP_RELAY,
                hops=[s.node.node_id, target_node_id],
                first_host=s.node.host,
                first_port=s.node.port,
            )))
    except Exception:
        pass

    # 5. MULTI_HOP_RELAY — represented as a single Route entry; the
    #    cluster_mesh recursive engine handles the actual chain.
    for s in (rank_proxies(candidates, top_k=4) if candidates else []):
        out.append(table.upsert(Route(
            target_node_id=target_node_id,
            route_type=RouteType.MULTI_HOP_RELAY,
            hops=[s.node.node_id, "...", target_node_id],
            first_host=s.node.host,
            first_port=s.node.port,
        )))

    # 6. CACHED_FALLBACK — last-known-good from disk cache.
    try:
        from app.services.peer_registry import peer_registry as pr
        cached = await pr.get(target_node_id)
        if cached and cached.host and cached.host != target.host:
            out.append(table.upsert(Route(
                target_node_id=target_node_id,
                route_type=RouteType.CACHED_FALLBACK,
                hops=[target_node_id],
                first_host=cached.host,
                first_port=getattr(cached, "port", target.port),
            )))
    except Exception:
        pass

    # 7. FEDERATION / REVERSE_TUNNEL / HOLE_PUNCH / RENDEZVOUS_HINT —
    #    only synthesise when the corresponding subsystem is actually
    #    reachable, otherwise the scorer wastes work on routes the
    #    executor will reject. Each gate is best-effort: import errors
    #    just skip the class, never the rest of route discovery.
    candidate_classes: list[RouteType] = []
    try:
        from app.services.federation_service import federation_service
        if federation_service._enabled():
            candidate_classes.append(RouteType.FEDERATION)
    except Exception:
        pass
    try:
        from app.nat.rendezvous_client import is_configured as _rdv_configured
        if _rdv_configured():
            candidate_classes.append(RouteType.REVERSE_TUNNEL)
            candidate_classes.append(RouteType.RENDEZVOUS_HINT)
    except Exception:
        pass
    try:
        from app.nat.nat_detector import get_nat_detector
        nt = get_nat_detector().current()
        # HOLE_PUNCH only makes sense when both ends are behind NAT
        # (full-cone / port-restricted). Symmetric NAT can't be hole-
        # punched reliably; OPEN doesn't need it.
        if nt and str(nt.value).lower() not in {"open", "symmetric", "unknown"}:
            candidate_classes.append(RouteType.HOLE_PUNCH)
    except Exception:
        pass

    for rt in candidate_classes:
        out.append(table.upsert(Route(
            target_node_id=target_node_id,
            route_type=rt,
            hops=[target_node_id],
            first_host=target.host,
            first_port=target.port,
        )))

    return out


# ── Scoring ────────────────────────────────────────────────────


def _hops_factor(hops: int) -> float:
    """1 hop = 1.0, 4 hops = 0.4, 5+ = 0.2 — penalises long chains."""
    if hops <= 1:
        return 1.0
    return max(0.2, 1.0 - 0.2 * (hops - 1))


def _route_class_floor(rt: RouteType) -> float:
    """Static priority floor — even with bad live data, RENDEZVOUS_HINT
    shouldn't outrank DIRECT on a healthy LAN."""
    return {
        RouteType.DIRECT:           1.00,
        RouteType.LAN_ALIAS:        0.95,
        RouteType.BRIDGE:           0.85,
        RouteType.SINGLE_HOP_RELAY: 0.75,
        RouteType.MULTI_HOP_RELAY:  0.65,
        RouteType.FEDERATION:       0.55,
        RouteType.CACHED_FALLBACK:  0.50,
        RouteType.REVERSE_TUNNEL:   0.40,
        RouteType.HOLE_PUNCH:       0.30,
        RouteType.RENDEZVOUS_HINT:  0.25,
    }.get(rt, 0.5)


def score_route(r: Route) -> tuple[float, dict]:
    """Compute a scalar score for a route plus a breakdown dict.

    A score of 0 means "rejected — don't even try". Anything > 0 is a
    valid candidate; the caller picks the highest.
    """
    breakdown = {"class_floor": _route_class_floor(r.route_type)}

    # Cooldown check — short-circuit reject.
    if r.is_in_cooldown():
        return 0.0, {**breakdown, "rejected": "in_cooldown"}

    # Trust check on the first hop.
    try:
        from app.services.trust_score import get_trust_db
        first_id = r.hops[0] if r.hops else ""
        if first_id and first_id != r.target_node_id:
            trust = get_trust_db().get_score(first_id)
            if trust < TRUST_REJECT_THRESHOLD:
                return 0.0, {**breakdown, "rejected": "low_trust"}
            breakdown["trust"] = trust
        else:
            trust = 0.6
            breakdown["trust"] = trust
    except Exception:
        trust = 0.5

    # Phi accrual — is the first hop suspected dead?
    try:
        from app.services.phi_accrual import get_phi_registry
        first_id = r.hops[0] if r.hops else r.target_node_id
        phi = get_phi_registry().detector_for(first_id).phi()
        if phi >= PHI_REJECT_THRESHOLD:
            return 0.0, {**breakdown, "rejected": f"phi={phi:.1f}"}
        breakdown["phi"] = round(phi, 2)
    except Exception:
        pass

    # Latency.
    try:
        from app.services.path_health import get_path_health
        latency_score = get_path_health().latency_score(r.first_host, r.first_port)
    except Exception:
        latency_score = 1.0
    breakdown["latency"] = round(latency_score, 3)

    # Bandwidth (if measured).
    try:
        from app.services.bandwidth_probe import get_bandwidth
        mbps = get_bandwidth().get(r.first_host, r.first_port) or 0.0
        bw_score = min(1.0, mbps / 100.0)  # 100 mbps → 1.0
    except Exception:
        bw_score = 0.5
    breakdown["bw"] = round(bw_score, 3)

    # Loss proxy = consecutive failures.
    loss_score = max(0.0, 1.0 - 0.2 * r.consecutive_failures)
    breakdown["loss"] = round(loss_score, 3)

    # Hop penalty.
    hops = _hops_factor(r.hop_count)
    breakdown["hops"] = round(hops, 3)

    # Route freshness (last_success).
    age_s = max(0.0, time.time() - r.last_success_at) if r.last_success_at else 9999.0
    age_score = 1.0 if age_s < 60 else 0.7 if age_s < 600 else 0.4
    breakdown["age"] = round(age_score, 3)

    # Load: backpressure on the *first hop* is what matters.
    load_score = 1.0
    try:
        if r.hops and r.hops[0] != r.target_node_id:
            from app.services.node_registry import compute_headroom, get_registry
            n = next(
                (x for x in get_registry().nodes(include_dead=True)
                 if x.node_id == r.hops[0]),
                None,
            )
            if n is not None:
                load_score = compute_headroom(n.load)
    except Exception:
        pass
    breakdown["load"] = round(load_score, 3)

    # Security: federation routes are HMAC-signed, give them a +.
    sec_score = (
        1.0 if r.route_type in (RouteType.FEDERATION,
                                 RouteType.REVERSE_TUNNEL)
        else 0.7
    )
    breakdown["security"] = sec_score

    # NAT-friendliness — DIRECT/LAN_ALIAS are best, HOLE_PUNCH worst.
    nat_score = {
        RouteType.DIRECT:           1.0,
        RouteType.LAN_ALIAS:        1.0,
        RouteType.BRIDGE:           0.9,
        RouteType.SINGLE_HOP_RELAY: 0.8,
        RouteType.MULTI_HOP_RELAY:  0.7,
        RouteType.FEDERATION:       0.7,
        RouteType.CACHED_FALLBACK:  0.6,
        RouteType.REVERSE_TUNNEL:   0.5,
        RouteType.HOLE_PUNCH:       0.4,
        RouteType.RENDEZVOUS_HINT:  0.4,
    }.get(r.route_type, 0.5)
    breakdown["nat"] = nat_score

    # Weighted sum × class floor.
    raw = (
        ROUTE_WEIGHTS["latency"]  * (latency_score / 2.0) +
        ROUTE_WEIGHTS["loss"]     * loss_score +
        ROUTE_WEIGHTS["bw"]       * bw_score +
        ROUTE_WEIGHTS["trust"]    * trust +
        ROUTE_WEIGHTS["load"]     * load_score +
        ROUTE_WEIGHTS["hops"]     * hops +
        ROUTE_WEIGHTS["age"]      * age_score +
        ROUTE_WEIGHTS["security"] * sec_score +
        ROUTE_WEIGHTS["nat"]      * nat_score
    )
    final = raw * _route_class_floor(r.route_type)
    breakdown["raw"] = round(raw, 4)
    breakdown["final"] = round(final, 4)

    r.score = final
    r.last_score_at = time.time()
    return final, breakdown


# ── Auto-strategy: pick which route classes to even try ─────────


def select_strategy() -> set[RouteType]:
    """Decide which route classes are *eligible* given live cluster
    conditions. We don't waste cycles enumerating tunnels when the
    LAN is healthy, and we don't bother probing DIRECT on a known-bad
    NAT path."""
    eligible = {RouteType.DIRECT, RouteType.LAN_ALIAS,
                RouteType.BRIDGE, RouteType.SINGLE_HOP_RELAY,
                RouteType.MULTI_HOP_RELAY, RouteType.CACHED_FALLBACK}

    # Backpressure REJECTED → skip DIRECT (we'd just choke ourselves).
    try:
        from app.services.backpressure import get_backpressure, BackpressureLevel
        if get_backpressure().snapshot().get("level") == BackpressureLevel.REJECTED.value:
            eligible.discard(RouteType.DIRECT)
            eligible.discard(RouteType.LAN_ALIAS)
    except Exception:
        pass

    # Partition / minority → prefer relay over direct (peer might be
    # on the other side and only reachable via bridge).
    try:
        from app.services.partition_detector import get_partition_state
        if not get_partition_state().is_majority():
            eligible.add(RouteType.FEDERATION)
            eligible.add(RouteType.REVERSE_TUNNEL)
    except Exception:
        pass

    # Cluster has Helen-Rendezvous reachable → unlock tunnel paths.
    try:
        import os
        if os.environ.get("HELEN_RENDEZVOUS_HOST"):
            eligible.add(RouteType.REVERSE_TUNNEL)
            eligible.add(RouteType.RENDEZVOUS_HINT)
    except Exception:
        pass

    return eligible


# ── Sender — pick top route, race timeout, fail over ────────────


async def send_via_route(
    r: Route,
    method: str,
    path: str,
    body: Any = None,
    headers: Optional[dict] = None,
) -> tuple[int, Any, dict]:
    """Dispatch one request through one route. Returns (status,
    body, response_headers)."""
    try:
        from app.services.cluster_mesh import relay_request
        from app.services.adaptive_timeout import timeout_for_peer
    except ImportError:
        return 503, {"error": "deps_missing"}, {}

    timeout = timeout_for_peer(r.first_host, r.first_port)

    if r.route_type in (RouteType.DIRECT, RouteType.LAN_ALIAS,
                         RouteType.CACHED_FALLBACK):
        # Direct hit on the target's host (no relay layer).
        try:
            import httpx
            url = f"http://{r.first_host}:{r.first_port}{path}"
            async with httpx.AsyncClient(timeout=timeout) as c:
                resp = await c.request(method, url, json=body,
                                       headers=headers or {})
            try:
                return resp.status_code, resp.json(), dict(resp.headers)
            except Exception:
                return resp.status_code, resp.text, dict(resp.headers)
        except Exception as e:
            return 502, {"error": str(e)[:80]}, {}

    # Everything else (BRIDGE / SINGLE_HOP / MULTI_HOP / FEDERATION /
    # tunnels) goes through cluster_mesh.relay_request which handles
    # the recursive logic.
    if r.route_type == RouteType.MULTI_HOP_RELAY:
        hops_remaining = 4
    elif r.route_type in (RouteType.SINGLE_HOP_RELAY, RouteType.BRIDGE):
        hops_remaining = 1
    else:
        hops_remaining = 2
    return await relay_request(
        target_node_id=r.target_node_id,
        method=method, path=path, body=body, headers=headers,
        timeout=timeout, hops_remaining=hops_remaining,
    )


def _record_outcome(r: Route, success: bool) -> None:
    now = time.time()
    r.last_used_at = now
    if success:
        r.last_success_at = now
        r.consecutive_failures = 0
        r.failed_until = 0.0
    else:
        r.last_failure_at = now
        r.consecutive_failures += 1
        r.failed_until = now + COOLDOWN_AFTER_FAIL_SEC


async def send(
    target_node_id: str,
    method: str = "GET",
    path: str = "/",
    body: Any = None,
    headers: Optional[dict] = None,
    *,
    max_attempts: int = 3,
) -> tuple[int, Any, dict]:
    """Adaptive multi-path send.

    1. Discover candidate routes for ``target_node_id``.
    2. Filter by ``select_strategy()`` (auto-mode based on conditions).
    3. Score every survivor and try the top-K in order until one
       returns a 2xx, capped at ``max_attempts``.
    4. Each failure is recorded so the next request avoids the bad
       route during its cooldown window.
    """
    routes = await discover_routes(target_node_id)
    if not routes:
        return 404, {"error": "no_routes", "target": target_node_id}, {}

    eligible_types = select_strategy()
    candidates = [r for r in routes if r.route_type in eligible_types]
    scored = []
    for r in candidates:
        s, _ = score_route(r)
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        return 503, {"error": "all_routes_rejected", "target": target_node_id}, {}

    last_err: tuple[int, Any, dict] = (502, {"error": "no_attempts"}, {})
    for attempt_idx, (_, route) in enumerate(scored[:max_attempts]):
        status, resp_body, resp_headers = await send_via_route(
            route, method, path, body, headers,
        )
        ok = 200 <= status < 300
        _record_outcome(route, ok)
        if ok:
            logger.debug(
                "multipath_send_ok",
                target=target_node_id[:24],
                attempt=attempt_idx,
                route_type=route.route_type.value,
                status=status,
            )
            return status, resp_body, resp_headers
        logger.debug(
            "multipath_send_failed_falling_over",
            target=target_node_id[:24],
            attempt=attempt_idx,
            route_type=route.route_type.value,
            status=status,
        )
        last_err = (status, resp_body, resp_headers)

    # All attempts failed — enqueue for background retry if available.
    try:
        from app.services.dead_letter_service import dead_letter_service
        await dead_letter_service.enqueue(
            kind="multipath_request",
            payload={
                "target_node_id": target_node_id,
                "method": method, "path": path, "body": body,
            },
        )
    except Exception:
        pass
    return last_err


# ── Background refresh loop — keeps the route table current ─────


_loop_task: Optional[asyncio.Task] = None
_running = False


async def _refresh_loop() -> None:
    global _running
    _running = True
    logger.info("multipath_refresh_started", interval_sec=REFRESH_INTERVAL_SEC)
    try:
        while _running:
            try:
                await _refresh_once()
            except Exception as e:
                logger.warning("multipath_refresh_failed", error=str(e))
            await asyncio.sleep(REFRESH_INTERVAL_SEC)
    finally:
        logger.info("multipath_refresh_stopped")


async def _refresh_once() -> None:
    """Re-enumerate routes for every fresh peer + score them so the
    table stays warm for the next ``send`` call."""
    try:
        from app.services.node_registry import get_registry
    except ImportError:
        return
    reg = get_registry()
    for n in reg.nodes(include_dead=False):
        if n.self_node:
            continue
        await discover_routes(n.node_id)
    # Evict cold routes.
    n = get_route_table().evict_stale(max_age_sec=600.0)
    if n:
        logger.debug("multipath_evict_stale", count=n)


def start_multipath_router() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(_refresh_loop(), name="multipath-router")
    except RuntimeError:
        logger.warning("multipath_router_no_event_loop_yet")


def stop_multipath_router() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


# ── Diagnostic snapshot ─────────────────────────────────────────


def snapshot() -> dict:
    """Full route-table dump for the admin UI."""
    table = get_route_table()
    routes = []
    for r in table.all():
        score, breakdown = score_route(r)
        routes.append({
            "target_node_id":       r.target_node_id,
            "route_type":           r.route_type.value,
            "hops":                 r.hops,
            "first_host":           r.first_host,
            "first_port":           r.first_port,
            "score":                round(score, 4),
            "in_cooldown":          r.is_in_cooldown(),
            "consecutive_failures": r.consecutive_failures,
            "last_success_age_s":   round(time.time() - r.last_success_at, 1)
                if r.last_success_at else None,
            "breakdown":            breakdown,
        })
    routes.sort(key=lambda x: x["score"], reverse=True)
    return {
        "strategy":  sorted(t.value for t in select_strategy()),
        "weights":   ROUTE_WEIGHTS,
        "thresholds": {
            "phi_reject":    PHI_REJECT_THRESHOLD,
            "trust_reject":  TRUST_REJECT_THRESHOLD,
            "cooldown_sec":  COOLDOWN_AFTER_FAIL_SEC,
        },
        "routes":    routes,
    }
