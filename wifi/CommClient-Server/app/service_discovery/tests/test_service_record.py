"""Tests for ServiceRecord serialisation + predicates."""

from __future__ import annotations

import time

from app.service_discovery.service_record import (
    ServiceRecord, ServiceStatus, ServiceType,
)


def test_record_defaults_have_unique_service_id():
    a = ServiceRecord()
    b = ServiceRecord()
    assert a.service_id != b.service_id
    assert a == a
    assert hash(a) == hash(a)


def test_record_serialisation_roundtrip():
    r = ServiceRecord(
        service_id="abc",
        service_type=ServiceType.RELAY,
        host="10.0.0.1", port=3000,
        cluster_id="x", region="us-east", zone="a",
        max_capacity=100, current_load=20,
        capabilities={"tls": True},
        tags={"primary"},
    )
    d = r.to_dict()
    back = ServiceRecord.from_dict(d)
    assert back.service_id == "abc"
    assert back.service_type is ServiceType.RELAY
    assert back.host == "10.0.0.1"
    assert back.region == "us-east"
    assert back.tags == {"primary"}


def test_alive_predicate_with_grace():
    r = ServiceRecord(ttl_sec=10, last_heartbeat_at=time.time() - 5)
    assert r.is_alive(grace_sec=5)
    r.last_heartbeat_at = time.time() - 30
    assert not r.is_alive(grace_sec=5)


def test_remaining_capacity_and_headroom():
    r = ServiceRecord(max_capacity=100, current_load=25)
    assert r.remaining_capacity() == 75
    assert r.headroom_pct() == 75.0


def test_remaining_capacity_zero_when_max_zero():
    r = ServiceRecord(max_capacity=0, current_load=10)
    assert r.remaining_capacity() == 0


def test_beat_updates_heartbeat_and_status():
    r = ServiceRecord(status=ServiceStatus.REGISTERING)
    old_hb = r.last_heartbeat_at
    time.sleep(0.001)
    r.beat(current_load=5)
    assert r.last_heartbeat_at > old_hb
    assert r.current_load == 5
    # REGISTERING auto-promotes to HEALTHY on first beat.
    assert r.status is ServiceStatus.HEALTHY


def test_beat_keeps_status_when_explicitly_passed():
    r = ServiceRecord(status=ServiceStatus.HEALTHY)
    r.beat(status=ServiceStatus.DEGRADED)
    assert r.status is ServiceStatus.DEGRADED


def test_to_dict_includes_derived_fields():
    r = ServiceRecord(max_capacity=100, current_load=30)
    d = r.to_dict()
    assert d["remaining_capacity"] == 70
    assert d["headroom_pct"] == 70.0


def test_status_enum_values_complete():
    """Sanity — all 6 statuses present."""
    expected = {
        "registering", "healthy", "degraded",
        "unhealthy", "draining", "dead",
    }
    assert {s.value for s in ServiceStatus} == expected


def test_service_type_enum_completeness():
    """All 12 service types should round-trip through value strings."""
    for st in ServiceType:
        assert ServiceType(st.value) is st
