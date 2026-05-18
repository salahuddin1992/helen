"""Load-balancing strategy — capacity-aware contributions.

Reads ``services.load_balancer.score_proxy`` for each candidate's
first hop (when the hop is a peer node) and turns it into a
``load`` contribution. Candidates routed via a saturated peer get
demoted; candidates via an idle high-capacity peer get a boost.
"""

from __future__ import annotations

from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.route_candidate import RouteCandidate

NAME = "load_balancing"


def evaluate(ctx: RoutingContext, candidates: list[RouteCandidate]) -> None:
    try:
        from app.services.node_registry import get_registry
        from app.services.load_balancer import score_proxy
    except Exception:
        return

    reg = get_registry()
    nodes_by_id = {n.node_id: n for n in reg.nodes(include_dead=True)}

    for c in candidates:
        if c.rejected:
            continue
        first = c.first_hop
        node = nodes_by_id.get(first) if first and first != ctx.target_node_id else None
        if node is None:
            c.add_contribution("load", 0.7)
            continue
        try:
            scored = score_proxy(node)
            c.add_contribution("load", round(min(1.0, scored.weight), 4))
            c.annotate("load_breakdown", scored.breakdown)
        except Exception:
            c.add_contribution("load", 0.5)
