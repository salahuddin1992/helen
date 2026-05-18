"""Tests for the service registry."""

from __future__ import annotations

import pytest

from app.service_discovery.discovery_exceptions import (
    ServiceNotFoundError, ServiceRegistrationError,
)
from app.service_discovery.service_record import (
    ServiceRecord, ServiceStatus, ServiceType,
)
from app.service_discovery.service_registry import (
    ServiceRegistry, get_registry,
)
from app.service_discovery.service_signing import sign_record


def _signed(service_id: str, host: str = "1.1.1.1",
            type_: ServiceType = ServiceType.RELAY) -> ServiceRecord:
    r = ServiceRecord(
        service_id=service_id,
        service_type=type_,
        server_id="peer-X",
        host=host, port=3000,
        cluster_id="default",
    )
    sign_record(r)
    return r


def test_singleton_identity():
    assert get_registry() is ServiceRegistry.instance()


def test_register_then_get():
    reg = ServiceRegistry()
    r = _signed("test-A")
    reg.register(r)
    assert reg.get("test-A") is not None


def test_register_rejects_missing_host():
    reg = ServiceRegistry()
    r = ServiceRecord(service_id="bad")
    sign_record(r)
    with pytest.raises(ServiceRegistrationError):
        reg.register(r)


def test_register_rejects_unsigned():
    reg = ServiceRegistry()
    r = ServiceRecord(
        service_id="unsigned",
        service_type=ServiceType.RELAY,
        host="1.1.1.1", port=3000,
    )
    with pytest.raises(ServiceRegistrationError):
        reg.register(r)


def test_register_updates_existing():
    reg = ServiceRegistry()
    r1 = _signed("update-test", host="1.1.1.1")
    r2 = _signed("update-test", host="2.2.2.2")
    reg.register(r1)
    reg.register(r2)
    got = reg.get("update-test")
    assert got is not None
    assert got.host == "2.2.2.2"


def test_heartbeat_refreshes():
    reg = ServiceRegistry()
    r = _signed("hb-test")
    reg.register(r)
    reg.heartbeat("hb-test", current_load=42)
    got = reg.get("hb-test")
    assert got is not None
    assert got.current_load == 42


def test_heartbeat_unknown_raises():
    reg = ServiceRegistry()
    with pytest.raises(ServiceNotFoundError):
        reg.heartbeat("does-not-exist")


def test_deregister_returns_bool():
    reg = ServiceRegistry()
    reg.register(_signed("dereg-test"))
    assert reg.deregister("dereg-test") is True
    assert reg.deregister("dereg-test") is False


def test_by_type_filter():
    reg = ServiceRegistry()
    reg.register(_signed("t-relay", type_=ServiceType.RELAY))
    reg.register(_signed("t-sig",   type_=ServiceType.SIGNALING))
    relays = reg.by_type(ServiceType.RELAY)
    sigs = reg.by_type(ServiceType.SIGNALING)
    assert any(r.service_id == "t-relay" for r in relays)
    assert any(r.service_id == "t-sig" for r in sigs)
    assert not any(r.service_id == "t-relay" for r in sigs)


def test_stats_shape():
    reg = ServiceRegistry()
    reg.register(_signed("stats-1"))
    s = reg.stats()
    expected = {"total", "by_type", "regions", "healthy", "stale"}
    assert expected.issubset(s.keys())
    assert s["total"] >= 1
