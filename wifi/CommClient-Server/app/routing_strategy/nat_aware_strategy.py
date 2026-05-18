"""NAT-aware strategy — promote routes that work behind specific NAT
types and demote ones that won't.

Without explicit NAT detection per peer (Helen-Server is LAN-first),
we use route-class priors:

  * DIRECT / LAN_ALIAS  → highest NAT score (they're inside our LAN)
  * BRIDGE              → high (a Helen we know about)
  * PROXY / RELAY       → medium (depend on chain integrity)
  * REVERSE_TUNNEL      → low (works only when rendezvous is up)
  * HOLE_PUNCH          → very low (fails on symmetric NAT)
"""

from __future__ import annotations

from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.route_candidate import RouteCandidate

NAME = "nat_aware"


_NAT_FRIENDLINESS = {
    "direct":           1.0,
    "lan_alias":        1.0,
    "bridge":           0.9,
    "single_hop_relay": 0.8,
    "multi_hop_relay":  0.7,
    "federation":       0.7,
    "cached_fallback":  0.6,
    "reverse_tunnel":   0.5,
    "hole_punch":       0.4,
    "rendezvous_hint":  0.4,
}


def evaluate(ctx: RoutingContext, candidates: list[RouteCandidate]) -> None:
    rendezvous_up = ctx.rendezvous_available
    for c in candidates:
        if c.rejected:
            continue
        score = _NAT_FRIENDLINESS.get(c.route_type, 0.5)
        # If rendezvous is reachable, give tunnel routes a small bump.
        if rendezvous_up and c.route_type in ("reverse_tunnel", "rendezvous_hint"):
            score = min(1.0, score + 0.2)
        c.add_contribution("nat", round(score, 3))
