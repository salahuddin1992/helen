"""Tests for adaptive_strategy — uses a fresh metrics state."""

from __future__ import annotations

from dataclasses import dataclass

from app.routing_strategy.adaptive_strategy import evaluate, NAME, _success_rate
from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.route_metrics import StrategyMetrics
from app.routing_strategy.routing_context import RoutingContext
from app.routing_strategy.strategy_config import reload_config


@dataclass
class _T: value: str
@dataclass
class _R:
    route_type: _T
    hops: list


def _c(rt: str) -> RouteCandidate:
    return RouteCandidate(route=_R(route_type=_T(rt), hops=["t"]))


def test_success_rate_returns_one_when_no_data():
    # Fresh metrics instance simulates a clean process.
    sm = StrategyMetrics()
    StrategyMetrics._singleton = sm
    rate = _success_rate()
    assert rate == 1.0


def test_evaluate_writes_adaptive_bias_contribution():
    reload_config()
    StrategyMetrics._singleton = StrategyMetrics()
    ctx = RoutingContext(target_node_id="t")
    c = _c("direct")
    evaluate(ctx, [c])
    assert "adaptive_bias" in c.contributions


def test_evaluate_skips_when_disabled(monkeypatch):
    monkeypatch.setenv("HELEN_RS_ADAPTIVE", "false")
    reload_config()
    ctx = RoutingContext(target_node_id="t")
    c = _c("direct")
    evaluate(ctx, [c])
    assert "adaptive_bias" not in c.contributions
    monkeypatch.delenv("HELEN_RS_ADAPTIVE", raising=False)
    reload_config()


def test_strategy_name_constant():
    assert NAME == "adaptive"
