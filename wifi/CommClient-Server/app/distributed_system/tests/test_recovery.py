"""Tests for distributed_system.recovery_manager."""

from __future__ import annotations

from app.distributed_system.distributed_events import emit
from app.distributed_system.recovery_manager import (
    RecoveryManager, get_recovery_manager,
)


def test_singleton_identity():
    assert get_recovery_manager() is RecoveryManager.instance()


def test_member_evicted_records_action():
    rm = RecoveryManager()
    rm._subscribe()
    before = rm.stats()["actions_taken"]
    emit("member.evicted", {"node_id": "test-evicted-node"})
    after = rm.stats()["actions_taken"]
    assert after > before


def test_consensus_failed_records_action():
    rm = RecoveryManager()
    rm._subscribe()
    before = rm.stats()["actions_taken"]
    emit("consensus.failed", {"kind": "test", "key": "k"})
    after = rm.stats()["actions_taken"]
    assert after > before


def test_stats_shape():
    rm = RecoveryManager()
    s = rm.stats()
    assert "actions_taken" in s
    assert "last_action_at" in s
    assert "subscribed" in s
