"""Tests for app.p2p.peer_identity."""

from __future__ import annotations

from app.p2p.peer_identity import (
    fingerprint, identity_snapshot, is_self, my_cluster_id, my_peer_id,
)


def test_fingerprint_stable_for_same_input():
    a = fingerprint("peer-X")
    b = fingerprint("peer-X")
    assert a == b
    assert len(a) == 16


def test_fingerprint_changes_with_pubkey():
    a = fingerprint("peer-X", "pub1")
    b = fingerprint("peer-X", "pub2")
    assert a != b


def test_my_peer_id_returns_string():
    pid = my_peer_id()
    assert isinstance(pid, str)
    assert len(pid) > 0


def test_my_cluster_id_returns_default_or_set():
    cid = my_cluster_id()
    assert isinstance(cid, str)
    assert len(cid) > 0


def test_is_self_truthy_for_my_id():
    assert is_self(my_peer_id())
    assert not is_self("definitely-not-me-xx")


def test_identity_snapshot_keys():
    s = identity_snapshot()
    assert {"peer_id", "cluster_id", "fingerprint"}.issubset(s.keys())
