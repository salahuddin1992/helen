"""Tests for load_balancing_strategy — works even without live registry."""

from __future__ import annotations

from dataclasses import dataclass

from app.routing_strategy.load_balancing_strategy import evaluate, NAME
from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.routing_context import RoutingContext


@dataclass
class _T: value: str
@dataclass
class _R:
    route_type: _T
    hops: list


def test_evaluate_handles_unknown_first_hop():
    """First hop isn't in node_registry — strategy should fall back."""
    ctx = RoutingContext(target_node_id="unknown_target")
    c = RouteCandidate(route=_R(route_type=_T("single_hop_relay"),
                                hops=["unknown_proxy", "unknown_target"]))
    evaluate(ctx, [c])
    assert "load" in c.contributions
    # Score should be the fallback (~0.5..0.7).
    assert 0.4 <= c.contributions["load"] <= 0.95


def test_evaluate_skips_rejected():
    ctx = RoutingContext(target_node_id="t")
    c = RouteCandidate(route=_R(route_type=_T("direct"), hops=["t"]))
    c.reject("test")
    evaluate(ctx, [c])
    assert "load" not in c.contributions


def test_evaluate_self_target_no_first_hop():
    ctx = RoutingContext(target_node_id="t")
    c = RouteCandidate(route=_R(route_type=_T("direct"), hops=["t"]))
    evaluate(ctx, [c])
    assert "load" in c.contributions  # still scored


def test_strategy_name_constant():
    assert NAME == "load_balancing"
