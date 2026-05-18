"""
Unit tests for the FileAcceptance state machine.

Stays out of the DB — tests the model methods directly so the state
transition invariants are verified regardless of storage backend.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.file_acceptance import (
    STATE_ACCEPTED,
    STATE_DELIVERED,
    STATE_PENDING,
    STATE_REJECTED,
    TERMINAL_STATES,
    VALID_STATES,
    FileAcceptance,
)


def _new(state: str = STATE_PENDING, bytes_received: int = 0) -> FileAcceptance:
    row = FileAcceptance()
    row.file_id = "f1"
    row.recipient_id = "u1"
    row.channel_id = "c1"
    row.state = state
    row.bytes_received = bytes_received
    row.delivered_at = None
    row.acted_at = None
    return row


class TestStateEnum:
    def test_valid_set_contents(self):
        assert VALID_STATES == {
            STATE_PENDING, STATE_DELIVERED, STATE_ACCEPTED, STATE_REJECTED,
        }

    def test_terminal_is_subset(self):
        assert TERMINAL_STATES <= VALID_STATES
        assert TERMINAL_STATES == {STATE_ACCEPTED, STATE_REJECTED}


class TestMarkDelivered:
    def test_pending_advances_to_delivered(self):
        row = _new()
        advanced = row.mark_delivered(bytes_received=100)
        assert advanced is True
        assert row.state == STATE_DELIVERED
        assert row.delivered_at is not None
        assert row.bytes_received == 100

    def test_second_call_does_not_advance_but_updates_bytes(self):
        row = _new()
        row.mark_delivered(bytes_received=100)
        first_ts = row.delivered_at
        advanced = row.mark_delivered(bytes_received=200)
        assert advanced is False
        assert row.delivered_at == first_ts  # idempotent
        assert row.bytes_received == 200

    def test_bytes_received_never_goes_backwards(self):
        row = _new()
        row.mark_delivered(bytes_received=500)
        row.mark_delivered(bytes_received=100)  # earlier stale value
        assert row.bytes_received == 500

    def test_terminal_states_are_frozen(self):
        row_acc = _new(state=STATE_ACCEPTED, bytes_received=10)
        advanced = row_acc.mark_delivered(bytes_received=99)
        assert advanced is False
        assert row_acc.state == STATE_ACCEPTED
        assert row_acc.bytes_received == 99  # but bytes still update

        row_rej = _new(state=STATE_REJECTED, bytes_received=0)
        advanced = row_rej.mark_delivered(bytes_received=50)
        assert advanced is False
        assert row_rej.state == STATE_REJECTED


class TestMarkAccepted:
    def test_from_pending(self):
        row = _new()
        advanced = row.mark_accepted()
        assert advanced is True
        assert row.state == STATE_ACCEPTED
        assert row.delivered_at is not None
        assert row.acted_at is not None

    def test_from_delivered(self):
        row = _new(state=STATE_DELIVERED)
        row.delivered_at = datetime.now(timezone.utc)
        advanced = row.mark_accepted()
        assert advanced is True
        assert row.state == STATE_ACCEPTED

    def test_double_accept_is_noop(self):
        row = _new(state=STATE_ACCEPTED)
        advanced = row.mark_accepted()
        assert advanced is False

    def test_rejection_blocks_acceptance(self):
        row = _new(state=STATE_REJECTED)
        advanced = row.mark_accepted()
        assert advanced is False
        assert row.state == STATE_REJECTED


class TestMarkRejected:
    def test_from_pending(self):
        row = _new()
        advanced = row.mark_rejected()
        assert advanced is True
        assert row.state == STATE_REJECTED
        assert row.acted_at is not None

    def test_from_delivered(self):
        row = _new(state=STATE_DELIVERED)
        advanced = row.mark_rejected()
        assert advanced is True
        assert row.state == STATE_REJECTED

    def test_already_accepted_blocks_reject(self):
        row = _new(state=STATE_ACCEPTED)
        advanced = row.mark_rejected()
        assert advanced is False
        assert row.state == STATE_ACCEPTED

    def test_double_reject_is_noop(self):
        row = _new(state=STATE_REJECTED)
        advanced = row.mark_rejected()
        assert advanced is False


class TestSerialization:
    def test_to_dict_contains_expected_fields(self):
        row = _new()
        d = row.to_dict()
        for key in (
            "file_id", "recipient_id", "channel_id",
            "state", "delivered_at", "acted_at", "bytes_received",
        ):
            assert key in d
        assert d["state"] == STATE_PENDING
        assert d["bytes_received"] == 0

    def test_to_dict_serializes_timestamps(self):
        row = _new()
        row.mark_accepted()
        d = row.to_dict()
        assert isinstance(d["delivered_at"], str)
        assert isinstance(d["acted_at"], str)


class TestInvariants:
    def test_accepted_implies_delivered_at_set(self):
        row = _new()
        row.mark_accepted()
        assert row.delivered_at is not None

    def test_pending_has_no_timestamps(self):
        row = _new()
        assert row.delivered_at is None
        assert row.acted_at is None

    def test_rejected_has_acted_at_but_maybe_no_delivered_at(self):
        row = _new()
        row.mark_rejected()
        assert row.acted_at is not None
        # Recipient can reject without ever downloading.
        assert row.delivered_at is None
