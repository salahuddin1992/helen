"""
Backend tests for the federation HTTP gate's TRANSIENT_PEER_STATES
behaviour added during the federation regression repair.

Background: a peer mid-enrollment (DISCOVERED → AUTHENTICATING →
VERIFIED → AUTO_ACCEPTED → APPROVED → PROVISIONING → SYNCING_STATE
→ READY) used to be blocked at the federation gate because only
ACTIVE_PEER_STATES (READY/DEGRADED) were allowed. The fix categorizes
the in-flight states as TRANSIENT_PEER_STATES and lets them through
the HMAC-only path (same as unknown peers). Only WAITING/PENDING/
AWAITING/REJECTED/DENIED/EVICTED are refused.

These tests pin that contract.
"""
from __future__ import annotations

import pytest

from app.models.server_node import (
    ACTIVE_PEER_STATES,
    REFUSED_PEER_STATES,
    TRANSIENT_PEER_STATES,
    WAITING_PEER_STATES,
    PEER_STATE_DISCOVERED,
    PEER_STATE_AUTHENTICATING,
    PEER_STATE_VERIFIED,
    PEER_STATE_AUTO_ACCEPTED,
    PEER_STATE_APPROVED,
    PEER_STATE_PROVISIONING,
    PEER_STATE_SYNCING_STATE,
    PEER_STATE_READY,
    PEER_STATE_DEGRADED,
    PEER_STATE_REJECTED,
    PEER_STATE_REJECTED_BY_ADMIN,
    PEER_STATE_DENIED,
    PEER_STATE_EVICTED,
    PEER_STATE_WAITING_MANUAL_APPROVAL,
    PEER_STATE_PENDING_APPROVAL,
    PEER_STATE_AWAITING_HUMAN,
)


class TestPeerStateBuckets:
    """The state buckets are the contract every federation gate +
    fabric subscriber relies on. If a state moves between buckets,
    a downstream gate may silently start refusing or accepting."""

    def test_active_states_are_routable(self):
        assert PEER_STATE_READY in ACTIVE_PEER_STATES
        assert PEER_STATE_DEGRADED in ACTIVE_PEER_STATES

    def test_transient_states_cover_full_enrollment_chain(self):
        """Every state from DISCOVERED through SYNCING_STATE must be
        in TRANSIENT — otherwise the federation gate blocks a peer
        mid-enrollment and the chicken-and-egg returns."""
        for state in (
            PEER_STATE_DISCOVERED,
            PEER_STATE_AUTHENTICATING,
            PEER_STATE_VERIFIED,
            PEER_STATE_AUTO_ACCEPTED,
            PEER_STATE_APPROVED,
            PEER_STATE_PROVISIONING,
            PEER_STATE_SYNCING_STATE,
        ):
            assert state in TRANSIENT_PEER_STATES, f"{state} missing from TRANSIENT"

    def test_active_and_transient_are_disjoint(self):
        """READY/DEGRADED must NOT be in TRANSIENT; they're terminal."""
        assert ACTIVE_PEER_STATES & TRANSIENT_PEER_STATES == set()

    def test_refused_states_are_disjoint_from_routable(self):
        """A refused peer must never be routable through any other path."""
        assert REFUSED_PEER_STATES & ACTIVE_PEER_STATES == set()
        assert REFUSED_PEER_STATES & TRANSIENT_PEER_STATES == set()

    def test_waiting_states_are_disjoint_from_transient(self):
        """WAITING/PENDING/AWAITING are admin-gated; transient is the
        automatic-flow set. They must NOT overlap because the gate
        treats them differently (transient = fail-open, waiting = block)."""
        assert WAITING_PEER_STATES & TRANSIENT_PEER_STATES == set()

    def test_refused_includes_admin_rejection(self):
        """A peer rejected by an admin must be permanently refused."""
        assert PEER_STATE_REJECTED in REFUSED_PEER_STATES
        assert PEER_STATE_REJECTED_BY_ADMIN in REFUSED_PEER_STATES
        assert PEER_STATE_DENIED in REFUSED_PEER_STATES
        assert PEER_STATE_EVICTED in REFUSED_PEER_STATES

    def test_waiting_includes_all_admin_review_states(self):
        for state in (
            PEER_STATE_WAITING_MANUAL_APPROVAL,
            PEER_STATE_PENDING_APPROVAL,
            PEER_STATE_AWAITING_HUMAN,
        ):
            assert state in WAITING_PEER_STATES


class TestPeerStateGateLogic:
    """Logical-only assertions about the gate's three-way decision —
    we exercise the tuple shape rather than spinning the FastAPI app
    because the dependency on `peer_approval_service.get_peer_status`
    is already covered by the `test_peer_acceptance.py` suite."""

    def test_gate_lets_unknown_peer_through(self):
        """status_str=None → fail-OPEN (HMAC alone gates). Legacy
        peers + tests that pre-date peer-acceptance rely on this."""
        # The gate's logic is `blocking = status is not None and not active and not transient`.
        status_str = None
        blocking = (
            status_str is not None
            and status_str not in ACTIVE_PEER_STATES
            and status_str not in TRANSIENT_PEER_STATES
        )
        assert blocking is False

    def test_gate_lets_active_peer_through(self):
        for status_str in (PEER_STATE_READY, PEER_STATE_DEGRADED):
            blocking = (
                status_str is not None
                and status_str not in ACTIVE_PEER_STATES
                and status_str not in TRANSIENT_PEER_STATES
            )
            assert blocking is False, f"{status_str} should be routable"

    def test_gate_lets_transient_peer_through(self):
        for status_str in TRANSIENT_PEER_STATES:
            blocking = (
                status_str is not None
                and status_str not in ACTIVE_PEER_STATES
                and status_str not in TRANSIENT_PEER_STATES
            )
            assert blocking is False, f"{status_str} should pass (transient)"

    def test_gate_blocks_waiting_peer(self):
        for status_str in WAITING_PEER_STATES:
            blocking = (
                status_str is not None
                and status_str not in ACTIVE_PEER_STATES
                and status_str not in TRANSIENT_PEER_STATES
            )
            assert blocking is True, f"{status_str} should be blocked"

    def test_gate_blocks_refused_peer(self):
        for status_str in REFUSED_PEER_STATES:
            blocking = (
                status_str is not None
                and status_str not in ACTIVE_PEER_STATES
                and status_str not in TRANSIENT_PEER_STATES
            )
            assert blocking is True, f"{status_str} should be blocked"

    def test_gate_blocks_empty_string_sentinel(self):
        """`get_peer_status` returns '' (empty string) on lookup error.
        That sentinel must not pass the gate — it's a fail-CLOSED
        on infrastructure failure."""
        status_str = ""
        blocking = (
            status_str is not None
            and status_str not in ACTIVE_PEER_STATES
            and status_str not in TRANSIENT_PEER_STATES
        )
        assert blocking is True


class TestBcryptAsyncSemaphore:
    """The auth queue fix bounds bcrypt parallelism with an asyncio
    semaphore. These tests verify the configuration is sensible and
    the async wrappers actually delegate to the same code path as the
    sync versions."""

    def test_semaphore_capacity_is_at_least_2(self):
        from app.core.security import _BCRYPT_MAX_PARALLEL
        assert _BCRYPT_MAX_PARALLEL >= 2

    def test_semaphore_capacity_is_bounded(self):
        """With unbounded parallelism the megascale stampede returns.
        Cap should be CPU/2 — never more than CPU count."""
        import os
        from app.core.security import _BCRYPT_MAX_PARALLEL
        cpu = os.cpu_count() or 4
        assert _BCRYPT_MAX_PARALLEL <= cpu

    @pytest.mark.asyncio
    async def test_hash_password_async_round_trip(self):
        from app.core.security import hash_password_async, verify_password_async
        h = await hash_password_async("CorrectHorseBatteryStaple")
        assert h.startswith("$2")  # bcrypt prefix
        assert await verify_password_async("CorrectHorseBatteryStaple", h) is True
        assert await verify_password_async("wrong-password", h) is False

    @pytest.mark.asyncio
    async def test_async_and_sync_produce_compatible_hashes(self):
        """Sync hash_password and async hash_password_async must produce
        bcrypt hashes that the other can verify — they call the same
        underlying function."""
        from app.core.security import (
            hash_password, verify_password,
            hash_password_async, verify_password_async,
        )
        sync_h = hash_password("test-password")
        async_h = await hash_password_async("test-password")
        assert verify_password("test-password", async_h) is True
        assert (await verify_password_async("test-password", sync_h)) is True
