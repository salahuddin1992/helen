"""Tests for distributed_system.cluster_membership."""

from __future__ import annotations

from app.distributed_system.cluster_membership import (
    ClusterMembership, get_cluster_membership,
)


def test_singleton_identity():
    assert get_cluster_membership() is ClusterMembership.instance()


def test_check_once_returns_expected_keys():
    cm = ClusterMembership()
    out = cm.check_once()
    assert "joined" in out
    assert "left" in out
    assert "evicted" in out
    assert "fresh" in out
    assert "known" in out


def test_members_is_a_set():
    cm = ClusterMembership()
    cm.check_once()
    members = cm.members()
    assert isinstance(members, set)


def test_idempotent_second_check_no_joins():
    cm = ClusterMembership()
    first = cm.check_once()
    second = cm.check_once()
    # Stable cluster: no new joins on the second tick.
    assert second["joined"] == []
