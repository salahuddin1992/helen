"""Tests for route_scoring_engine."""

from __future__ import annotations

from dataclasses import dataclass

from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.route_scoring_engine import (
    aggregate_contributions,
    compose_score,
    score_all,
    _CLASS_FLOOR,
)
from app.routing_strategy.strategy_config import StrategyConfig


# ── Fakes ────────────────────────────────────────────────────────


@dataclass
class _FakeRouteType:
    value: str


@dataclass
class _FakeRoute:
    route_type: _FakeRouteType
    hops: list = None
    first_host: str = ""
    first_port: int = 0
    last_success_at: float = 0.0
    consecutive_failures: int = 0
    failed_until: float = 0.0


def _candidate(route_type: str, weight: float = 0.0) -> RouteCandidate:
    r = _FakeRoute(route_type=_FakeRouteType(route_type), hops=["x"])
    return RouteCandidate(route=r, weight=weight)


# ── Tests ───────────────────────────────────────────────────────


def test_aggregate_contributions_uses_config_weights():
    c = _candidate("direct")
    c.add_contribution("latency", 0.8)
    c.add_contribution("trust",   1.0)
    cfg = StrategyConfig()
    val = aggregate_contributions(c, cfg)
    expected = cfg.w_latency * 0.8 + cfg.w_trust * 1.0
    assert abs(val - expected) < 1e-6


def test_compose_score_applies_class_floor():
    c1 = _candidate("direct")
    c2 = _candidate("rendezvous_hint")
    c1.add_contribution("latency", 1.0)
    c2.add_contribution("latency", 1.0)
    cfg = StrategyConfig()
    s1 = compose_score(c1, cfg)
    s2 = compose_score(c2, cfg)
    assert s1 > s2
    assert c1.annotations["class_floor"] == _CLASS_FLOOR["direct"]
    assert c2.annotations["class_floor"] == _CLASS_FLOOR["rendezvous_hint"]


def test_score_all_skips_rejected():
    a = _candidate("direct"); a.add_contribution("latency", 1.0)
    b = _candidate("direct"); b.reject("test")
    score_all([a, b])
    assert a.weight > 0
    assert b.weight == 0


def test_unknown_contribution_key_uses_default_weight():
    c = _candidate("direct")
    c.add_contribution("unknown_key", 1.0)
    val = aggregate_contributions(c, StrategyConfig())
    assert val > 0  # uses fallback weight 0.05
