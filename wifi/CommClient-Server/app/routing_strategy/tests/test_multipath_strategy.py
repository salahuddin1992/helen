"""Tests for multipath_strategy — independent of live services."""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.routing_strategy.multipath_strategy import (
    evaluate, NAME, _hop_factor, _age_factor,
)
from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.routing_context import RoutingContext


@dataclass
class _T: value: str
@dataclass
class _R:
    route_type: _T
    hops: list
    first_host: str = ""
    first_port: int = 0
    last_success_at: float = 0.0
    consecutive_failures: int = 0


def _c(rt: str, hops: list[str], **kw) -> RouteCandidate:
    return RouteCandidate(route=_R(route_type=_T(rt), hops=hops, **kw))


# ── Pure helpers ────────────────────────────────────────────────


def test_hop_factor_one_hop_full():
    assert _hop_factor(1) == 1.0


def test_hop_factor_decays_with_more_hops():
    assert _hop_factor(2) < _hop_factor(1)
    assert _hop_factor(4) < _hop_factor(2)
    assert _hop_factor(10) >= 0.2  # floor


def test_age_factor_zero_when_never_proven():
    assert _age_factor(0) == 0.4


def test_age_factor_full_when_recent():
    assert _age_factor(time.time()) == 1.0


def test_age_factor_decays_with_time():
    assert _age_factor(time.time() - 30)  == 1.0
    assert _age_factor(time.time() - 300) == 0.7
    assert _age_factor(time.time() - 3000) == 0.4


# ── Evaluate ────────────────────────────────────────────────────


def test_evaluate_assigns_all_expected_contributions():
    ctx = RoutingContext(target_node_id="t")
    c = _c("direct", ["t"])
    evaluate(ctx, [c])
    expected = {"latency", "hops", "age", "loss", "security"}
    assert expected.issubset(c.contributions.keys())


def test_evaluate_skips_rejected():
    ctx = RoutingContext(target_node_id="t")
    c = _c("direct", ["t"])
    c.reject("test")
    evaluate(ctx, [c])
    assert "latency" not in c.contributions


def test_evaluate_security_score_higher_for_federation():
    ctx = RoutingContext(target_node_id="t")
    fed = _c("federation", ["t"])
    direct = _c("direct", ["t"])
    evaluate(ctx, [fed, direct])
    assert fed.contributions["security"] > direct.contributions["security"]


def test_strategy_name_constant():
    assert NAME == "multipath"
