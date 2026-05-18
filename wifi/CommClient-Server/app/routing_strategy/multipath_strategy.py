"""Multipath strategy — fold live latency + hop count + age into the
candidate weight.

This is where the strategy package meets ``path_health`` /
``adaptive_timeout`` / route-age signals. A candidate with a fast,
fresh, low-hop path scores high; one with a slow stale path scores
low.

This strategy is named ``multipath`` because its job is to make
multipath routing meaningful: without it every candidate would
look identical to the scoring engine.
"""

from __future__ import annotations

import time

from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.route_health import get_health_view
from app.routing_strategy.routing_context import RoutingContext

NAME = "multipath"


def _hop_factor(hop_count: int) -> float:
    if hop_count <= 1:
        return 1.0
    return max(0.2, 1.0 - 0.2 * (hop_count - 1))


def _age_factor(last_success_at: float) -> float:
    if last_success_at <= 0:
        return 0.4   # never proven
    age = time.time() - last_success_at
    if age < 60:
        return 1.0
    if age < 600:
        return 0.7
    return 0.4


def evaluate(ctx: RoutingContext, candidates: list[RouteCandidate]) -> None:
    health = get_health_view()
    for c in candidates:
        if c.rejected:
            continue
        host = getattr(c.route, "first_host", "")
        port = int(getattr(c.route, "first_port", 0) or 0)
        # Latency contribution.
        if host and port:
            lat = health.latency_score(host, port)
        else:
            lat = 1.0
        c.add_contribution("latency", round(min(2.0, lat) / 2.0, 4))
        # Bandwidth (if probed).
        if host and port:
            bw = health.bandwidth_mbps(host, port)
            if bw is not None:
                c.add_contribution("bandwidth", round(min(1.0, bw / 100.0), 4))
                c.annotate("bandwidth_mbps", round(bw, 2))
        # Hops + age + loss.
        c.add_contribution("hops", round(_hop_factor(c.hop_count), 4))
        last_ok = float(getattr(c.route, "last_success_at", 0.0) or 0.0)
        c.add_contribution("age", round(_age_factor(last_ok), 4))
        fails = int(getattr(c.route, "consecutive_failures", 0) or 0)
        loss = max(0.0, 1.0 - 0.2 * fails)
        c.add_contribution("loss", round(loss, 4))
        # Security: federation/tunnel routes carry HMAC + signed chains.
        sec = 1.0 if c.route_type in ("federation", "reverse_tunnel") else 0.7
        c.add_contribution("security", sec)
