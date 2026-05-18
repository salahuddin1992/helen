"""Tests for trust_aware_strategy."""

from __future__ import annotations

from dataclasses import dataclass

from app.routing_strategy.trust_aware_strategy import evaluate, NAME
from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.routing_context import RoutingContext


@dataclass
class _T: value: str
@dataclass
class _R:
    route_type: _T
    hops: list


def test_evaluate_assigns_trust_for_first_hop():
    ctx = RoutingContext(target_node_id="t")
    c = RouteCandidate(route=_R(route_type=_T("single_hop_relay"),
                                hops=["proxy-X", "t"]))
    evaluate(ctx, [c])
    assert "trust" in c.contributions
    assert 0.0 <= c.contributions["trust"] <= 1.0


def test_evaluate_self_target_uses_default_trust():
    ctx = RoutingContext(target_node_id="t")
    c = RouteCandidate(route=_R(route_type=_T("direct"), hops=["t"]))
    evaluate(ctx, [c])
    assert "trust" in c.contributions


def test_evaluate_skips_rejected():
    ctx = RoutingContext(target_node_id="t")
    c = RouteCandidate(route=_R(route_type=_T("direct"), hops=["t"]))
    c.reject("test")
    evaluate(ctx, [c])
    assert "trust" not in c.contributions


def test_strategy_name_constant():
    assert NAME == "trust_aware"
