"""Tests for app.monitoring.dashboard_renderer."""

from __future__ import annotations

from app.monitoring.dashboard_renderer import (
    aggregate_state, render_json, render_text, render_mermaid_summary,
)


def test_aggregate_state_returns_expected_keys():
    s = aggregate_state()
    expected = {"ts", "health", "metrics", "alerts", "latency", "topology"}
    assert expected.issubset(s.keys())


def test_render_json_returns_dict():
    s = render_json()
    assert isinstance(s, dict)
    assert "ts" in s


def test_render_text_contains_headers():
    text = render_text()
    assert isinstance(text, str)
    assert "HELEN MONITORING DASHBOARD" in text
    assert "[health]" in text
    assert "[alerts]" in text


def test_render_mermaid_summary_pie_format():
    text = render_mermaid_summary()
    assert text.startswith("pie title Helen Health")
    assert "Healthy checks" in text
    assert "Failing checks" in text


def test_render_text_handles_no_state_gracefully():
    """Even with empty subsystems, rendering should not raise."""
    text = render_text()
    assert text  # non-empty
