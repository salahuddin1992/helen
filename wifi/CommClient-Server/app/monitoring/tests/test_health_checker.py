"""Tests for app.monitoring.health_checker."""

from __future__ import annotations

from app.monitoring.health_checker import HealthChecker, get_health_checker


def test_singleton_identity():
    assert get_health_checker() is HealthChecker.instance()


def test_run_all_returns_expected_shape():
    hc = HealthChecker()
    snap = hc.run_all()
    assert "ok" in snap
    assert "ok_count" in snap
    assert "total_checks" in snap
    assert "checks" in snap
    assert isinstance(snap["checks"], dict)


def test_register_custom_check_runs():
    hc = HealthChecker()
    hc.register("custom_test", lambda: (True, "always ok"))
    snap = hc.run_all()
    assert "custom_test" in snap["checks"]
    assert snap["checks"]["custom_test"]["ok"] is True
    hc.unregister("custom_test")


def test_failing_check_makes_overall_fail():
    hc = HealthChecker()
    hc.register("always_fail", lambda: (False, "test fail"))
    snap = hc.run_all()
    assert snap["ok"] is False
    assert snap["checks"]["always_fail"]["ok"] is False
    hc.unregister("always_fail")


def test_history_grows_capped():
    hc = HealthChecker()
    for _ in range(5):
        hc.run_all()
    h = hc.history(limit=10)
    assert len(h) >= 1
    assert all("ok" in row for row in h)


def test_raising_check_treated_as_failure():
    hc = HealthChecker()

    def raises():
        raise RuntimeError("boom")

    hc.register("raises_test", raises)
    snap = hc.run_all()
    assert snap["checks"]["raises_test"]["ok"] is False
    assert "raised" in snap["checks"]["raises_test"]["detail"]
    hc.unregister("raises_test")
