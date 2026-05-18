"""Trust-aware strategy — incorporates peer reputation.

Reads ``services.trust_score`` for each candidate's first hop and
adds a contribution proportional to the score. Hard rejection of
quarantined peers is handled by ``route_constraints``; this strategy
shapes the *preference* among the survivors.
"""

from __future__ import annotations

from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.route_candidate import RouteCandidate

NAME = "trust_aware"


def evaluate(ctx: RoutingContext, candidates: list[RouteCandidate]) -> None:
    try:
        from app.services.trust_score import get_trust_db
        db = get_trust_db()
    except Exception:
        return

    for c in candidates:
        if c.rejected:
            continue
        first = c.first_hop
        score = db.get_score(first) if first and first != ctx.target_node_id else 0.7
        c.add_contribution("trust", round(float(score), 4))
