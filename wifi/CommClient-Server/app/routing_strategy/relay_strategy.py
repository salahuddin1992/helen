"""Relay strategy — annotate routes with their relay-suitability.

Relay routes (BRIDGE / SINGLE_HOP / MULTI_HOP) earn a small bonus
when the local backpressure is REJECTED — at that point the local
host shouldn't try direct delivery, so relay candidates are
strictly more useful.
"""

from __future__ import annotations

from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.route_candidate import RouteCandidate

NAME = "relay"

_RELAY_TYPES = {"bridge", "single_hop_relay", "multi_hop_relay"}


def evaluate(ctx: RoutingContext, candidates: list[RouteCandidate]) -> None:
    bp = ctx.backpressure_level
    bonus = 1.0 if bp == "rejected" else (0.7 if bp == "degraded" else 0.5)
    for c in candidates:
        if c.rejected:
            continue
        score = bonus if c.route_type in _RELAY_TYPES else 0.5
        c.add_contribution("relay_fit", round(score, 3))
