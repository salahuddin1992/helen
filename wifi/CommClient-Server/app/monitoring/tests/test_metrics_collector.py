"""Tests for app.monitoring.metrics_collector."""

from __future__ import annotations

from app.monitoring.metrics_collector import (
    MetricsCollector, get_metrics_collector,
)


def test_singleton_identity():
    assert get_metrics_collector() is MetricsCollector.instance()


def test_collect_once_returns_subsystem_keys():
    mc = MetricsCollector()
    snap = mc.collect_once()
    expected = {
        "ts", "path_health", "backpressure", "partition",
        "multipath", "routing_strategy", "distributed",
    }
    assert expected.issubset(snap.keys())


def test_latest_returns_collected_data():
    mc = MetricsCollector()
    mc.collect_once()
    latest = mc.latest()
    assert "ts" in latest
    assert isinstance(latest, dict)


def test_collected_at_updates_on_collect():
    mc = MetricsCollector()
    before = mc.collected_at()
    mc.collect_once()
    after = mc.collected_at()
    assert after >= before
