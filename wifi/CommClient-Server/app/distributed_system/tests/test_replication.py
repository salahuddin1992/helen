"""Tests for distributed_system.replication_manager facade."""

from __future__ import annotations

from app.distributed_system import replication_manager as rep


def test_put_and_get_roundtrip():
    rec = rep.put_replicated("ds_test_kind", "ds_test_key", {"hello": "world"})
    assert rec["version"] >= 1
    back = rep.get_replicated("ds_test_kind", "ds_test_key")
    assert back is not None
    assert back["value"] == {"hello": "world"}


def test_get_unknown_returns_none():
    assert rep.get_replicated("ds_test_kind", "no_such_key_xxx") is None


def test_stats_returns_local_count():
    s = rep.stats()
    assert "local_records" in s
    assert s["local_records"] >= 0
