"""Federation strategy — handles cross-cluster routes.

Federation candidates are demoted in normal operation (LAN-first
philosophy) but promoted heavily when:

  * The local cluster is in minority (partition).
  * The target's cluster_id differs from ours.
"""

from __future__ import annotations

from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.route_candidate import RouteCandidate

NAME = "federation"


def evaluate(ctx: RoutingContext, candidates: list[RouteCandidate]) -> None:
    minority = not ctx.is_majority
    for c in candidates:
        if c.rejected:
            continue
        if c.route_type == "federation":
            # In a minority partition, federation routes are the only
            # way to talk to the other side — score high.
            score = 0.95 if minority else 0.4
        else:
            score = 0.6
        c.add_contribution("federation_fit", round(score, 3))
