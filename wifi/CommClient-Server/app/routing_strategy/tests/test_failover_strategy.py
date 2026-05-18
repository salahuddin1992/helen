"""Tests for failover_strategy."""

from __future__ import annotations

from dataclasses import dataclass

from app.routing_strategy.failover_strategy import evaluate, NAME
from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.routing_context import RoutingContext


@dataclass
class _T: value: str
@dataclass
class _R:
    route_type: _T
    hops: list


def _c(first_hop: str) -> RouteCandidate:
    return RouteCandidate(
        route=_R(route_type=_T("single_hop_relay"), hops=[first_hop, "target"]),
    )


def test_failover_unique_first_hops_score_full():
    ctx = RoutingContext(target_node_id="target")
    cs = [_c("p1"), _c("p2"), _c("p3")]
    evaluate(ctx, cs)
    for c in cs:
        assert c.contributions["fallback_fit"] >= 0.99


def test_failover_duplicate_first_hops_decay():
    ctx = RoutingContext(target_node_id="target")
    cs = [_c("p1"), _c("p1"), _c("p1"), _c("p1")]
    evaluate(ctx, cs)
    scores = [c.contributions["fallback_fit"] for c in cs]
    # Each subsequent duplicate gets a smaller score, floor 0.3.
    assert scores[0] >= scores[1] >= scores[2] >= scores[3]
    assert min(scores) >= 0.3


def test_failover_strategy_name_constant():
    assert NAME == "failover"
