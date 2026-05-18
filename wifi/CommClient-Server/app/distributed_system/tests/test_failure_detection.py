"""Tests for distributed_system.failure_detector facade."""

from __future__ import annotations

from app.distributed_system import failure_detector as fd


def test_unknown_node_is_alive_default():
    """A never-heard-from node has no inter-arrival samples → φ=0
    (effectively healthy)."""
    assert fd.is_alive("never-seen-node-xx") is True


def test_suspect_level_returns_float():
    val = fd.suspect_level("anything")
    assert isinstance(val, float)
    assert val >= 0


def test_snapshot_returns_dict():
    s = fd.snapshot()
    assert isinstance(s, dict)


def test_evict_unknown_does_not_raise():
    fd.evict("does-not-exist")
