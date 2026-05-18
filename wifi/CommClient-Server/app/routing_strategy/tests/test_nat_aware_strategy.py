"""Tests for nat_aware_strategy."""

from __future__ import annotations

from dataclasses import dataclass

from app.routing_strategy.nat_aware_strategy import (
    evaluate, NAME, _NAT_FRIENDLINESS,
)
from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.routing_context import RoutingContext


@dataclass
class _T: value: str
@dataclass
class _R:
    route_type: _T
    hops: list


def _c(rt: str) -> RouteCandidate:
    return RouteCandidate(route=_R(route_type=_T(rt), hops=["t"]))


def test_direct_higher_than_hole_punch():
    ctx = RoutingContext(target_node_id="t")
    direct = _c("direct")
    hp     = _c("hole_punch")
    evaluate(ctx, [direct, hp])
    assert direct.contributions["nat"] > hp.contributions["nat"]


def test_rendezvous_bonus_when_available():
    ctx_yes = RoutingContext(target_node_id="t", rendezvous_available=True)
    ctx_no  = RoutingContext(target_node_id="t", rendezvous_available=False)
    c1 = _c("reverse_tunnel")
    c2 = _c("reverse_tunnel")
    evaluate(ctx_yes, [c1])
    evaluate(ctx_no,  [c2])
    assert c1.contributions["nat"] > c2.contributions["nat"]


def test_evaluate_skips_rejected():
    ctx = RoutingContext(target_node_id="t")
    c = _c("direct")
    c.reject("test")
    evaluate(ctx, [c])
    assert "nat" not in c.contributions


def test_friendliness_table_has_all_route_types():
    expected = {
        "direct", "lan_alias", "bridge", "single_hop_relay",
        "multi_hop_relay", "federation", "cached_fallback",
        "reverse_tunnel", "hole_punch", "rendezvous_hint",
    }
    assert expected == set(_NAT_FRIENDLINESS.keys())


def test_strategy_name_constant():
    assert NAME == "nat_aware"
