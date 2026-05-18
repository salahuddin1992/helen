"""
Tests for the HTTP upload throttle.

Covers:
  - Event count sliding window
  - Byte quota sliding window
  - Concurrency slot cap
  - Failure rollback (release success=False)
  - release_inflight retains window entry
  - Oversize single upload guard
  - Multi-user isolation
  - Context-manager convenience wrapper
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.upload_throttle import (
    ThrottleError,
    UploadThrottle,
    _UploadSlot,
)


@pytest.fixture
def throttle() -> UploadThrottle:
    return UploadThrottle(
        max_files=3,
        max_bytes=1000,
        window_seconds=60,
        max_concurrent=2,
    )


# ── Event count ─────────────────────────────────────────────────────

class TestEventCount:
    @pytest.mark.asyncio
    async def test_allows_up_to_limit(self, throttle):
        for _ in range(3):
            await throttle.acquire("u1", 10)
            await throttle.release_inflight("u1")
        # state: 3 events in window, 0 in-flight
        assert throttle.stats("u1")["events"] == 3

    @pytest.mark.asyncio
    async def test_rejects_over_limit(self, throttle):
        for _ in range(3):
            await throttle.acquire("u1", 10)
            await throttle.release_inflight("u1")
        with pytest.raises(ThrottleError) as ei:
            await throttle.acquire("u1", 10)
        assert "count" in ei.value.reason
        assert ei.value.retry_after_seconds is not None


# ── Byte quota ──────────────────────────────────────────────────────

class TestByteQuota:
    @pytest.mark.asyncio
    async def test_allows_up_to_byte_cap(self, throttle):
        await throttle.acquire("u1", 400)
        await throttle.release_inflight("u1")
        await throttle.acquire("u1", 600)
        await throttle.release_inflight("u1")
        assert throttle.stats("u1")["window_bytes"] == 1000

    @pytest.mark.asyncio
    async def test_rejects_over_byte_cap(self, throttle):
        await throttle.acquire("u1", 600)
        await throttle.release_inflight("u1")
        with pytest.raises(ThrottleError) as ei:
            await throttle.acquire("u1", 500)
        assert "byte" in ei.value.reason.lower() or "quota" in ei.value.reason.lower()

    @pytest.mark.asyncio
    async def test_single_upload_oversize(self, throttle):
        with pytest.raises(ThrottleError) as ei:
            await throttle.acquire("u1", 2000)
        assert "exceeds" in ei.value.reason.lower()


# ── Concurrency ─────────────────────────────────────────────────────

class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_slot_cap(self, throttle):
        await throttle.acquire("u1", 10)
        await throttle.acquire("u1", 10)
        # max_concurrent=2 → next one should trip
        with pytest.raises(ThrottleError) as ei:
            await throttle.acquire("u1", 10)
        assert "concurrent" in ei.value.reason.lower()

    @pytest.mark.asyncio
    async def test_release_frees_slot(self, throttle):
        await throttle.acquire("u1", 10)
        await throttle.acquire("u1", 10)
        await throttle.release("u1", success=True)
        # Should now have one slot free again.
        await throttle.acquire("u1", 10)


# ── Rollback semantics ──────────────────────────────────────────────

class TestReleaseBehavior:
    @pytest.mark.asyncio
    async def test_failure_refunds_bytes(self, throttle):
        await throttle.acquire("u1", 900)
        # "upload failed" → the reservation must be rolled back.
        await throttle.release("u1", success=False)
        assert throttle.stats("u1")["window_bytes"] == 0
        # Full fresh allowance should be available.
        await throttle.acquire("u1", 900)

    @pytest.mark.asyncio
    async def test_success_retains_bytes(self, throttle):
        await throttle.acquire("u1", 900)
        await throttle.release("u1", success=True)
        assert throttle.stats("u1")["window_bytes"] == 900

    @pytest.mark.asyncio
    async def test_release_inflight_retains_window_entry(self, throttle):
        # Simulates resumable upload init: slot freed, quota still held.
        await throttle.acquire("u1", 400)
        await throttle.release_inflight("u1")
        st = throttle.stats("u1")
        assert st["in_flight"] == 0
        assert st["window_bytes"] == 400


# ── Multi-user isolation ────────────────────────────────────────────

class TestMultiUser:
    @pytest.mark.asyncio
    async def test_users_are_independent(self, throttle):
        # Exhaust u1.
        for _ in range(3):
            await throttle.acquire("u1", 100)
            await throttle.release_inflight("u1")
        with pytest.raises(ThrottleError):
            await throttle.acquire("u1", 100)

        # u2 should still have a fresh quota.
        for _ in range(3):
            await throttle.acquire("u2", 100)
            await throttle.release_inflight("u2")
        # u2's 4th would also fail — independent windows.
        with pytest.raises(ThrottleError):
            await throttle.acquire("u2", 100)


# ── Context manager ─────────────────────────────────────────────────

class TestContextManager:
    @pytest.mark.asyncio
    async def test_success_path(self, throttle):
        slot = _UploadSlot(throttle, "u1", 200)
        async with slot:
            slot.mark_success()
        assert throttle.stats("u1")["window_bytes"] == 200
        assert throttle.stats("u1")["in_flight"] == 0

    @pytest.mark.asyncio
    async def test_failure_refunds(self, throttle):
        slot = _UploadSlot(throttle, "u1", 200)
        with pytest.raises(RuntimeError):
            async with slot:
                raise RuntimeError("boom")
        assert throttle.stats("u1")["window_bytes"] == 0
        assert throttle.stats("u1")["in_flight"] == 0
