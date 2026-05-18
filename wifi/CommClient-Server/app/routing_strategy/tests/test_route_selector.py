"""Tests for route_selector."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.routing_strategy.route_candidate import RouteCandidate
from app.routing_strategy.route_selector import (
    select_top_k,
    split_primary_and_fallbacks,
    _tiebreak_key,
)
from app.routing_strategy.strategy_exceptions import AllRoutesRejectedError


@dataclass
class _T:
    value: str


@dataclass
class _R:
    route_type: _T
    hops: list


def _c(rt: str, weight: float, hops: list[str]) -> RouteCandidate:
    rc = RouteCandidate(route=_R(route_type=_T(rt), hops=hops))
    rc.weight = weight
    return rc


def test_select_top_k_orders_by_weight():
    a = _c("direct", 0.9, ["t"])
    b = _c("bridge", 0.7, ["b", "t"])
    c = _c("multi_hop_relay", 0.5, ["a", "b", "t"])
    out = select_top_k([c, a, b], k=3)
    assert [x.route_type for x in out] == ["direct", "bridge", "multi_hop_relay"]


def test_select_top_k_truncates_to_k():
    cands = [_c("direct", 1.0 - i * 0.1, ["t"]) for i in range(5)]
    out = select_top_k(cands, k=2)
    assert len(out) == 2


def test_select_top_k_raises_on_all_rejected():
    a = _c("direct", 0.0, ["t"])
    a.reject("test")
    b = _c("bridge", 0.0, ["t"])
    b.reject("test")
    with pytest.raises(AllRoutesRejectedError):
        select_top_k([a, b], k=2)


def test_tiebreak_prefers_fewer_hops():
    a = _c("direct", 0.5, ["t"])           # 1 hop
    b = _c("direct", 0.5, ["x", "y", "t"]) # 3 hops
    out = select_top_k([b, a], k=2)
    assert out[0] is a


def test_split_primary_and_fallbacks():
    a = _c("direct", 0.9, ["t"])
    b = _c("bridge", 0.7, ["b", "t"])
    primary, fallbacks = split_primary_and_fallbacks([a, b])
    assert primary is a
    assert fallbacks == [b]
