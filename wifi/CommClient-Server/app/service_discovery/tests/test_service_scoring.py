"""Tests for the composite scoring algorithm + locality bonus."""

from __future__ import annotations

from app.service_discovery.region_zone import (
    locality_bonus, my_region, my_zone,
)
from app.service_discovery.service_record import (
    ServiceRecord, ServiceStatus, ServiceType,
)
from app.service_discovery.service_scoring import (
    W_HEALTH, W_LATENCY, W_CAPACITY, W_LOCALITY, W_ADVERTISE, score,
)


def test_weights_sum_to_one():
    total = W_HEALTH + W_LATENCY + W_CAPACITY + W_LOCALITY + W_ADVERTISE
    assert abs(total - 1.0) < 1e-9


def test_score_returns_breakdown():
    r = ServiceRecord(
        service_id="x",
        service_type=ServiceType.RELAY,
        host="1.1.1.1", port=3000,
        max_capacity=100, current_load=10,
        status=ServiceStatus.HEALTHY,
    )
    s, breakdown = score(r)
    assert 0 < s <= 1.5
    expected = {"health", "latency", "capacity", "locality",
                "advertise", "final", "health_breakdown"}
    assert expected.issubset(breakdown.keys())


def test_full_capacity_outranks_loaded():
    base = dict(
        service_id="x", service_type=ServiceType.RELAY,
        host="1.1.1.1", port=3000,
        max_capacity=100, status=ServiceStatus.HEALTHY,
    )
    empty = ServiceRecord(**base, current_load=0)
    full  = ServiceRecord(**{**base, "service_id": "y"}, current_load=99)
    s_empty, _ = score(empty)
    s_full,  _ = score(full)
    assert s_empty > s_full


def test_same_region_outranks_other_region():
    base = dict(
        service_type=ServiceType.RELAY,
        host="1.1.1.1", port=3000,
        max_capacity=100, current_load=10,
        status=ServiceStatus.HEALTHY,
    )
    same = ServiceRecord(service_id="same", region=my_region(),
                          zone=my_zone(), **base)
    other = ServiceRecord(service_id="other", region="far-away",
                           zone="far-zone", **base)
    s_same,  _ = score(same)
    s_other, _ = score(other)
    assert s_same > s_other


def test_unhealthy_status_drops_score():
    base = dict(
        service_id="u", service_type=ServiceType.RELAY,
        host="1.1.1.1", port=3000,
        max_capacity=100, current_load=10,
    )
    healthy = ServiceRecord(**base, status=ServiceStatus.HEALTHY)
    unhealthy = ServiceRecord(**{**base, "service_id": "u2"},
                                status=ServiceStatus.UNHEALTHY)
    s_h, _ = score(healthy)
    s_u, _ = score(unhealthy)
    assert s_h > s_u


def test_locality_bonus_zero_for_different_region():
    bonus = locality_bonus("region-a", "zone-a",
                            caller_region="region-b", caller_zone="zone-b")
    assert bonus == 0.0


def test_locality_bonus_positive_same_region():
    bonus = locality_bonus("rA", "zA", caller_region="rA", caller_zone="zB")
    assert bonus > 0
