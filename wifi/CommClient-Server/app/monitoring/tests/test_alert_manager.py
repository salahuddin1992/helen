"""Tests for app.monitoring.alert_manager."""

from __future__ import annotations

import pytest

from app.monitoring.alert_manager import AlertManager, get_alert_manager
from app.monitoring.monitoring_exceptions import AlertConfigError


def test_singleton_identity():
    assert get_alert_manager() is AlertManager.instance()


def test_register_invalid_rule_raises():
    am = AlertManager()
    with pytest.raises(AlertConfigError):
        am.register_rule("bad", "not_callable")  # type: ignore


def test_check_once_returns_results():
    am = AlertManager()
    am.register_rule("test_quiet", lambda: (False, "ok"))
    out = am.check_once()
    assert "checked" in out
    assert "firing" in out
    assert "results" in out
    assert "test_quiet" in out["results"]
    am.unregister_rule("test_quiet")


def test_state_transition_emits_changed():
    am = AlertManager()
    flips = {"v": False}

    def rule():
        return flips["v"], "test"

    am.register_rule("test_flip", rule)
    out1 = am.check_once()
    assert out1["results"]["test_flip"]["firing"] is False
    flips["v"] = True
    out2 = am.check_once()
    assert out2["results"]["test_flip"]["firing"] is True
    assert out2["results"]["test_flip"]["changed"] is True
    am.unregister_rule("test_flip")


def test_state_returns_history():
    am = AlertManager()
    am.register_rule("hist_test", lambda: (False, "stable"))
    am.check_once()
    am.check_once()
    s = am.state("hist_test")
    assert s is not None
    assert "history" in s
    assert len(s["history"]) >= 1
    am.unregister_rule("hist_test")


def test_state_unknown_returns_none():
    am = AlertManager()
    assert am.state("does_not_exist") is None
