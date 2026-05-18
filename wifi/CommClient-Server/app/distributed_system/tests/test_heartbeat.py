"""Tests for distributed_system.heartbeat_manager."""

from __future__ import annotations

from app.distributed_system.heartbeat_manager import (
    HeartbeatManager, get_heartbeat_manager,
)


def test_singleton_identity():
    assert get_heartbeat_manager() is HeartbeatManager.instance()


def test_beat_self_does_not_raise():
    HeartbeatManager().beat_self()


def test_beat_self_increments_phi_samples():
    """After a couple of beats with a small delay between them, phi
    accrual has at least one inter-arrival sample.

    The test resets the detector first so cross-test pollution
    (e.g. recovery_manager auto-evicting on breaker.open) doesn't
    leave the detector in an unexpected state.
    """
    import time
    from app.services.phi_accrual import get_phi_registry
    from app.distributed_system.node_identity import server_id
    pid = server_id()
    get_phi_registry().evict(pid)  # ensure a clean slate
    mgr = HeartbeatManager()
    mgr.beat_self()
    time.sleep(0.005)  # guarantee a non-zero inter-arrival
    mgr.beat_self()
    snap = get_phi_registry().detector_for(pid).snapshot()
    # Two heartbeats → 1 inter-arrival sample.
    assert snap["samples"] >= 1
