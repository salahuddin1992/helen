"""Route-scoring engine — combines strategy contributions into final weights.

Each strategy adds a contribution to a candidate via
``candidate.add_contribution(name, score)``. After every strategy
has run, the engine reduces those contributions to a single
``weight`` per candidate using a weighted sum + class-floor
multiplier.

Weights are pulled from ``strategy_config`` so operators can re-bias
the scorer without code changes. Class floors come from the
underlying Route class (DIRECT > BRIDGE > RELAY > ...).
"""

from __future__ import annotations

from typing import Iterable

from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.strategy_config import StrategyConfig, get_config


# Static priority floor per route class — same numbers as in
# multipath_router._route_class_floor for consistency.
_CLASS_FLOOR = {
    "direct":           1.00,
    "lan_alias":        0.95,
    "bridge":           0.85,
    "single_hop_relay": 0.75,
    "multi_hop_relay":  0.65,
    "federation":       0.55,
    "cached_fallback":  0.50,
    "reverse_tunnel":   0.40,
    "hole_punch":       0.30,
    "rendezvous_hint":  0.25,
}


def _floor_for(candidate: RouteCandidate) -> float:
    return _CLASS_FLOOR.get(candidate.route_type, 0.5)


def aggregate_contributions(c: RouteCandidate, cfg: StrategyConfig) -> float:
    """Reduce all per-strategy contributions into a [0, 1+]ish weight.

    Strategies prefix their contributions with their key from
    strategy_config (latency / loss / bw / trust / load / hops / age /
    security / nat). Unknown keys still contribute with weight 0.05.
    """
    total = 0.0
    for key, score in c.contributions.items():
        weight = getattr(cfg, f"w_{key}", 0.05)
        total += weight * float(score)
    return total


def compose_score(c: RouteCandidate,
                  cfg: StrategyConfig | None = None) -> float:
    """Compute and assign the final candidate.weight."""
    cfg = cfg or get_config()
    raw = aggregate_contributions(c, cfg)
    final = raw * _floor_for(c)
    c.weight = round(final, 6)
    c.annotations.setdefault("class_floor", _floor_for(c))
    c.annotations.setdefault("raw_score", round(raw, 6))
    return c.weight


def score_all(candidates: Iterable[RouteCandidate],
              cfg: StrategyConfig | None = None) -> list[RouteCandidate]:
    """Run compose_score across every non-rejected candidate."""
    cfg = cfg or get_config()
    out = []
    for c in candidates:
        if not c.rejected:
            compose_score(c, cfg)
        out.append(c)
    return out
