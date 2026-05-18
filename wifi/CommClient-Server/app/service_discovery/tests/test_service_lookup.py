"""Tests for find_best / find_top_k filter + ordering."""

from __future__ import annotations

import pytest

from app.service_discovery.discovery_exceptions import ServiceNotFoundError
from app.service_discovery.service_lookup import (
    find_best, find_failover_chain, find_top_k,
)
from app.service_discovery.service_record import (
    ServiceRecord, ServiceStatus, ServiceType,
)
from app.service_discovery.service_registry import get_registry
from app.service_discovery.service_signing import sign_record


_TEST_IDS = ("lkup-A", "lkup-B", "lkup-C", "lkup-D")


@pytest.fixture(autouse=True)
def _clean_registry():
    reg = get_registry()
    for tid in _TEST_IDS:
        reg.deregister(tid)
    yield
    for tid in _TEST_IDS:
        reg.deregister(tid)


def _seed(service_id: str, *, region="us-east", load=10,
          status=ServiceStatus.HEALTHY,
          type_=ServiceType.RELAY) -> ServiceRecord:
    r = ServiceRecord(
        service_id=service_id,
        service_type=type_,
        server_id=f"peer-{service_id}",
        host=f"10.0.0.{ord(service_id[-1]) % 250}", port=3000,
        cluster_id="default",
        region=region, zone="zone-a",
        ttl_sec=60, max_capacity=100, current_load=load,
        status=status,
    )
    sign_record(r)
    get_registry().register(r)
    return r


def test_find_best_returns_eligible_record():
    _seed("lkup-A")
    record, score, _ = find_best(ServiceType.RELAY)
    assert record.service_id == "lkup-A"
    assert score > 0


def test_find_best_raises_when_no_match():
    with pytest.raises(ServiceNotFoundError):
        find_best(ServiceType.STORAGE)  # nothing of this type registered


def test_find_top_k_orders_high_to_low():
    _seed("lkup-A", load=0)    # most headroom
    _seed("lkup-B", load=50)
    _seed("lkup-C", load=95)   # nearly full
    top = find_top_k(ServiceType.RELAY, k=3)
    # Score should descend.
    scores = [s for _, s, _ in top]
    assert scores == sorted(scores, reverse=True)


def test_find_top_k_filters_unhealthy():
    _seed("lkup-A", status=ServiceStatus.HEALTHY)
    _seed("lkup-B", status=ServiceStatus.DEAD)
    top = find_top_k(ServiceType.RELAY, k=5)
    ids = {r.service_id for r, _, _ in top}
    assert "lkup-A" in ids
    assert "lkup-B" not in ids


def test_region_filter_strict():
    _seed("lkup-A", region="us-east")
    _seed("lkup-B", region="eu-west")
    top = find_top_k(ServiceType.RELAY, k=5, region="eu-west")
    ids = {r.service_id for r, _, _ in top}
    assert ids == {"lkup-B"}


def test_failover_chain_returns_records():
    _seed("lkup-A", load=0)
    _seed("lkup-B", load=20)
    chain = find_failover_chain(ServiceType.RELAY, k=2)
    assert len(chain) == 2
    assert all(isinstance(r, ServiceRecord) for r in chain)


def test_capacity_floor_excludes_full():
    """Records below capacity_floor_pct should be rejected."""
    _seed("lkup-A", load=99)   # < 5% headroom
    # _seed defaults to max_capacity=100 → 1% headroom rejected.
    top = find_top_k(ServiceType.RELAY, k=5)
    ids = {r.service_id for r, _, _ in top}
    assert "lkup-A" not in ids
