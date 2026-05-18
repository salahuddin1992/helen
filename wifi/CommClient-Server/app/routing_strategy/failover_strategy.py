"""Failover strategy — annotate candidates with their fallback fitness.

The strategy package selects up to K candidates; the *order* among
them is the fallback chain when the primary fails. This strategy
contributes a small ``fallback_fit`` score that biases the selector
toward candidates that diversify the fallback (different first
hops, different route classes), giving us better resilience.
"""

from __future__ import annotations

from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.route_candidate import RouteCandidate

NAME = "failover"


def evaluate(ctx: RoutingContext, candidates: list[RouteCandidate]) -> None:
    """Penalise duplicate first-hops within the candidate list so the
    failover chain doesn't all share one peer."""
    seen_first_hops: dict[str, int] = {}
    for c in candidates:
        if c.rejected:
            continue
        first = c.first_hop
        seen = seen_first_hops.get(first, 0)
        # First occurrence: full score; subsequent: decaying.
        score = max(0.3, 1.0 - 0.25 * seen)
        seen_first_hops[first] = seen + 1
        c.add_contribution("fallback_fit", round(score, 3))
