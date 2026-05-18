"""Adaptive strategy — the meta-strategy.

Watches recent decision outcomes (via ``route_metrics``) and, when
the success rate drops below threshold, nudges the contributions
to break out of a bad pattern (e.g. prefer different route classes,
shrink top_k temporarily). This is where the package becomes
*adaptive* rather than just configurable.

Pure heuristic — no ML, deterministic given the same metrics.
"""

from __future__ import annotations

from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.route_metrics import get_metrics
from app.routing_strategy.strategy_config import get_config

NAME = "adaptive"


def _success_rate() -> float:
    snap = get_metrics().snapshot()
    counters = snap.get("counters", {})
    total = counters.get("decisions_total", 0)
    ok = counters.get("decisions_resolved", 0)
    if total < 5:
        return 1.0  # not enough data — assume healthy
    return ok / max(1, total)


def evaluate(ctx: RoutingContext, candidates: list[RouteCandidate]) -> None:
    cfg = get_config()
    if not cfg.adaptive_enabled:
        return
    rate = _success_rate()
    if rate >= 0.8:
        # Healthy — small confidence bump.
        for c in candidates:
            if c.rejected:
                continue
            c.add_contribution("adaptive_bias", 0.6)
        return

    # Degraded — bias contributions:
    #   * Boost route classes we haven't been picking lately.
    #   * Penalise direct routes (those have likely been failing).
    for c in candidates:
        if c.rejected:
            continue
        if c.route_type in ("direct", "lan_alias") and rate < 0.5:
            c.add_contribution("adaptive_bias", 0.3)
        else:
            c.add_contribution("adaptive_bias", 0.85)
        c.annotate("adaptive_success_rate", round(rate, 3))
