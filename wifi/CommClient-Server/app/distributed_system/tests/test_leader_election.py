"""Tests for distributed_system.leader_election facade."""

from __future__ import annotations

import inspect

from app.distributed_system import leader_election


def test_lead_is_async_context_manager():
    cm = leader_election.lead("test-name")
    assert inspect.isasyncgen(cm) or hasattr(cm, "__aenter__")


def test_status_returns_dict():
    s = leader_election.status("does-not-exist-yet")
    assert isinstance(s, dict)
    assert "name" in s
