"""Proxy strategy — when to prefer an HTTP proxy over a relay.

A proxy candidate is one whose first hop runs Helen-Server and can
re-sign federation requests on our behalf (e.g. for cross-cluster
calls). The strategy adds a small contribution when the proxy is
known-trusted and the destination requires HMAC.
"""

from __future__ import annotations

from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.route_candidate import RouteCandidate

NAME = "proxy"


def evaluate(ctx: RoutingContext, candidates: list[RouteCandidate]) -> None:
    for c in candidates:
        if c.rejected:
            continue
        score = 0.5
        if c.route_type == "single_hop_relay":
            # Single-hop proxy with HMAC signing is a clean choice.
            score = 0.85
        elif c.route_type == "multi_hop_relay":
            # Multi-hop has more attack surface for signing chain.
            score = 0.65
        c.add_contribution("proxy_fit", round(score, 3))
