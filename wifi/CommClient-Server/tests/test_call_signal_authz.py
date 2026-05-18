"""
Tests for the cross-server WebRTC signaling authorization shadow.

CallSignalAuthz lives in app.services.call_signal_authz and acts as a
minimal participant-set registry that lets a relay server authorize
WebRTC signal events (offer/answer/ice_candidate/call_signal) when the
authoritative ActiveCall sits on a *different* Helen server in the
federation.

What we cover:
  1. seed/add/remove/clear lifecycle behaviour
  2. expiry — stale entries return unauthorized
  3. self-signaling rejected
  4. apply_federation_event correctly maps wire events to operations
  5. participants() returns a defensive copy
"""

from __future__ import annotations

import time

import pytest

from app.services.call_signal_authz import (
    CallSignalAuthz,
    apply_federation_event,
    call_signal_authz,
)


# ── basic mutation ────────────────────────────────────────────────────


class TestSeedAndAuthorize:

    def test_seed_authorizes_pair(self):
        reg = CallSignalAuthz()
        reg.seed("c1", ["alice", "bob"])
        assert reg.is_authorized("c1", "alice", "bob") is True
        assert reg.is_authorized("c1", "bob", "alice") is True

    def test_unseeded_rejects(self):
        reg = CallSignalAuthz()
        assert reg.is_authorized("c1", "alice", "bob") is False

    def test_third_party_rejected(self):
        reg = CallSignalAuthz()
        reg.seed("c1", ["alice", "bob"])
        assert reg.is_authorized("c1", "alice", "carol") is False
        assert reg.is_authorized("c1", "eve", "bob") is False

    def test_self_signaling_rejected(self):
        reg = CallSignalAuthz()
        reg.seed("c1", ["alice", "bob"])
        assert reg.is_authorized("c1", "alice", "alice") is False

    def test_missing_inputs_rejected(self):
        reg = CallSignalAuthz()
        reg.seed("c1", ["alice", "bob"])
        assert reg.is_authorized("", "alice", "bob") is False
        assert reg.is_authorized("c1", "", "bob") is False
        assert reg.is_authorized("c1", "alice", "") is False


class TestAddRemove:

    def test_add_extends_existing(self):
        reg = CallSignalAuthz()
        reg.seed("c1", ["alice", "bob"])
        reg.add_participant("c1", "carol")
        assert reg.is_authorized("c1", "carol", "alice") is True
        assert reg.is_authorized("c1", "carol", "bob") is True

    def test_add_creates_new_entry(self):
        reg = CallSignalAuthz()
        # Out-of-order federation: peer_joined arrives before incoming.
        reg.add_participant("c1", "alice")
        reg.add_participant("c1", "bob")
        assert reg.is_authorized("c1", "alice", "bob") is True

    def test_remove_drops_one(self):
        reg = CallSignalAuthz()
        reg.seed("c1", ["alice", "bob", "carol"])
        reg.remove_participant("c1", "carol")
        assert reg.is_authorized("c1", "alice", "bob") is True
        assert reg.is_authorized("c1", "carol", "alice") is False

    def test_remove_last_clears_entry(self):
        reg = CallSignalAuthz()
        reg.seed("c1", ["alice"])
        reg.remove_participant("c1", "alice")
        assert reg.size() == 0

    def test_clear_drops_call(self):
        reg = CallSignalAuthz()
        reg.seed("c1", ["alice", "bob"])
        reg.clear("c1")
        assert reg.is_authorized("c1", "alice", "bob") is False
        assert reg.size() == 0


class TestExpiry:

    def test_expired_entry_treated_as_unauthorized(self):
        reg = CallSignalAuthz(ttl_seconds=0.01)
        reg.seed("c1", ["alice", "bob"])
        assert reg.is_authorized("c1", "alice", "bob") is True
        time.sleep(0.05)
        assert reg.is_authorized("c1", "alice", "bob") is False
        # Expired entry was lazily purged during the failed lookup.
        assert reg.size() == 0


class TestParticipantsSnapshot:

    def test_participants_returns_copy(self):
        reg = CallSignalAuthz()
        reg.seed("c1", ["alice", "bob"])
        snap = reg.participants("c1")
        snap.add("eve")
        # Internal set wasn't mutated by external caller.
        assert reg.is_authorized("c1", "eve", "alice") is False

    def test_participants_empty_when_unknown(self):
        reg = CallSignalAuthz()
        assert reg.participants("missing") == set()


# ── federation event mapping ──────────────────────────────────────────


class TestApplyFederationEvent:
    """The `apply_federation_event` helper is what the federation
    receive endpoint calls before fanning out to local sids. Each event
    name maps to a specific shadow operation."""

    def setup_method(self):
        # Reset the module-level singleton so tests don't bleed.
        call_signal_authz.clear("c-fed")
        call_signal_authz.clear("c-fed-2")

    def test_call_incoming_seeds(self):
        apply_federation_event("call_incoming", {
            "call_id": "c-fed",
            "caller_id": "alice",
            # The receiving server is delivering this to user "bob"; the
            # federation receive code knows the target_user_id but in
            # this helper we extract whoever appears in the payload.
            "callee_id": "bob",
        })
        assert call_signal_authz.is_authorized("c-fed", "alice", "bob") is True

    def test_call_peer_joined_extends(self):
        call_signal_authz.seed("c-fed", ["alice", "bob"])
        apply_federation_event("call_participant_joined", {
            "call_id": "c-fed",
            "user_id": "carol",
        })
        assert call_signal_authz.is_authorized("c-fed", "carol", "alice") is True

    def test_call_participant_left_removes(self):
        call_signal_authz.seed("c-fed", ["alice", "bob", "carol"])
        apply_federation_event("call_participant_left", {
            "call_id": "c-fed",
            "user_id": "carol",
        })
        assert call_signal_authz.is_authorized("c-fed", "carol", "alice") is False
        assert call_signal_authz.is_authorized("c-fed", "alice", "bob") is True

    def test_call_hangup_clears(self):
        call_signal_authz.seed("c-fed", ["alice", "bob"])
        apply_federation_event("call_hangup", {
            "call_id": "c-fed",
            "ended_by": "alice",
        })
        assert call_signal_authz.is_authorized("c-fed", "alice", "bob") is False

    def test_participants_list_dicts_supported(self):
        # call:peer_ready ships participants as list[str], but other
        # events ship list[{"user_id": "..."}]. Both should seed.
        apply_federation_event("call:peer_joined", {
            "call_id": "c-fed-2",
            "participants": [{"user_id": "alice"}, {"user_id": "bob"}],
        })
        assert call_signal_authz.is_authorized("c-fed-2", "alice", "bob") is True

    def test_unknown_event_is_noop(self):
        apply_federation_event("channel:message", {
            "call_id": "should-not-leak",
            "user_id": "alice",
        })
        assert call_signal_authz.is_authorized("should-not-leak", "alice", "bob") is False

    def test_missing_call_id_is_noop(self):
        # No call_id field — nothing to seed against.
        apply_federation_event("call_incoming", {
            "caller_id": "alice", "callee_id": "bob",
        })
        # Sanity: the registry doesn't grow.
        assert call_signal_authz.size() == 0


# ── integration shape: signal handler authorization fallback ─────────


class TestAuthorizeSignalFallback:
    """Validate the helper `_authorize_signal` from call_handlers in
    isolation: when call_service has no entry but the shadow has, it
    should still resolve a call_id."""

    def test_resolves_via_shadow_when_callservice_empty(self, monkeypatch):
        from app.socket import call_handlers

        # Patch call_service so it returns no local call.
        class _FakeCS:
            @staticmethod
            def get_user_call(uid):
                return None

        monkeypatch.setattr(call_handlers, "call_service", _FakeCS)

        # Seed only the shadow.
        call_signal_authz.clear("c-shadow-only")
        call_signal_authz.seed("c-shadow-only", ["alice", "bob"])

        cid = call_handlers._authorize_signal("alice", "bob")
        assert cid == "c-shadow-only"
        cid_other = call_handlers._authorize_signal("alice", "carol")
        assert cid_other is None
