"""Tests for app.monitoring.latency_tracker."""

from __future__ import annotations

import time

from app.monitoring.latency_tracker import (
    LatencyTracker, get_latency_tracker, time_op,
)


def test_singleton_identity():
    assert get_latency_tracker() is LatencyTracker.instance()


def test_record_then_stats_returns_count():
    lt = LatencyTracker()
    lt.reset("op_test_count")
    lt.record("op_test_count", 10)
    lt.record("op_test_count", 20)
    s = lt.stats("op_test_count")
    assert s["count"] == 2
    assert s["min"] == 10
    assert s["max"] == 20


def test_stats_unknown_op_count_zero():
    lt = LatencyTracker()
    s = lt.stats("never_recorded")
    assert s["count"] == 0


def test_p95_p99_present():
    lt = LatencyTracker()
    lt.reset("op_pct")
    for v in range(1, 101):
        lt.record("op_pct", v)
    s = lt.stats("op_pct")
    assert s["count"] == 100
    assert s["p95"] >= 90
    assert s["p99"] >= 95


def test_window_caps_samples():
    lt = LatencyTracker()
    lt.reset("op_cap")
    for v in range(2000):
        lt.record("op_cap", v)
    s = lt.stats("op_cap")
    # Window default 500 — no more than 500 samples kept.
    assert s["count"] <= 500


def test_time_op_records():
    lt = get_latency_tracker()
    lt.reset("ctx_test")
    with time_op("ctx_test"):
        time.sleep(0.001)
    s = lt.stats("ctx_test")
    assert s["count"] == 1
    assert s["min"] >= 0  # at minimum recorded the elapsed
