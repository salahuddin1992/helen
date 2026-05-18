"""Tests for app.p2p.peer_handshake."""

from __future__ import annotations

import time

import pytest

from app.p2p.p2p_exceptions import PeerHandshakeError
from app.p2p.peer_handshake import (
    announce_payload, perform_inbound_handshake, require_active,
    verify_announce,
)
from app.p2p.peer_lifecycle import PeerState, get_lifecycle


_TEST_PEER_IDS = ("peer-hs-ok", "peer-hs-bad")


def _reset_test_peer(peer_id: str) -> None:
    """Wipe persistent state for a test peer so the test is hermetic
    even after prior failed runs left rows in trust_db / sync_policy
    or stale lifecycle entries."""
    try:
        from app.services.trust_score import get_trust_db
        get_trust_db().reset(peer_id)
    except Exception:
        pass
    try:
        from app.services.sync_policy import get_sync_policy
        get_sync_policy().unblock(peer_id)
    except Exception:
        pass
    try:
        from app.p2p.peer_lifecycle import get_lifecycle
        get_lifecycle()._states.pop(peer_id, None)  # type: ignore[attr-defined]
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _scrub_test_peers():
    """Run before AND after every test in this module so prior runs
    or other test files can't leak state into peer-hs-* identifiers."""
    for pid in _TEST_PEER_IDS:
        _reset_test_peer(pid)
    yield
    for pid in _TEST_PEER_IDS:
        _reset_test_peer(pid)


def test_announce_payload_keys():
    p = announce_payload("peer-1", "default", "1.1.1.1", 3000)
    assert p["peer_id"] == "peer-1"
    assert p["cluster_id"] == "default"
    assert "ts" in p


def test_verify_ok_for_matching_cluster():
    p = announce_payload("peer-2", "default", "1.1.1.1", 3000)
    ok, reason = verify_announce(p, "default")
    assert ok is True


def test_verify_rejects_cluster_mismatch():
    p = announce_payload("peer-3", "other", "1.1.1.1", 3000)
    ok, reason = verify_announce(p, "default")
    assert ok is False
    assert reason == "cluster_mismatch"


def test_verify_rejects_stale_timestamp():
    p = announce_payload("peer-4", "default", "1.1.1.1", 3000)
    p["ts"] = int(time.time()) - 1000
    ok, reason = verify_announce(p, "default")
    assert ok is False
    assert reason == "stale_timestamp"


def test_verify_rejects_missing_peer_id():
    ok, reason = verify_announce({"cluster_id": "default"}, "default")
    assert ok is False
    assert reason == "missing_peer_id"


def test_inbound_handshake_promotes_to_active():
    # Use the live cluster_id so the test passes regardless of what
    # the surrounding test environment set.
    from app.p2p.peer_identity import my_cluster_id
    p = announce_payload("peer-hs-ok", my_cluster_id(), "1.1.1.1", 3000)
    assert perform_inbound_handshake(p) is True
    assert get_lifecycle().state("peer-hs-ok") is PeerState.ACTIVE


def test_inbound_handshake_rejects_bad_payload():
    p = announce_payload("peer-hs-bad", "definitely-not-this-cluster",
                          "1", 3000)
    assert perform_inbound_handshake(p) is False


def test_require_active_raises_for_unverified():
    with pytest.raises(PeerHandshakeError):
        require_active("never-handshaken-xx")
