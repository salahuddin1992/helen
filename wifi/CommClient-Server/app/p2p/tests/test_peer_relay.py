"""Tests for app.p2p.peer_relay (stats only — relay code is in services)."""

from __future__ import annotations

from app.p2p.peer_relay import RelayStats, get_relay_stats


def test_singleton_identity():
    assert get_relay_stats() is RelayStats.instance()


def test_record_increments_counters():
    rs = RelayStats()
    before_count = rs.snapshot()["count"]
    rs.record(success=True, chain_length=2)
    rs.record(success=False, chain_length=1)
    snap = rs.snapshot()
    assert snap["count"] == before_count + 2
    assert snap["success"] >= 1
    assert snap["failed"] >= 1


def test_avg_chain_length_computed():
    rs = RelayStats()
    rs.record(True, 2); rs.record(True, 4); rs.record(True, 6)
    snap = rs.snapshot()
    assert snap["avg_chain_length"] >= 2.0
