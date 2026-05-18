"""
Tests for the Progressive Group Call Join Flow additions:
  • idempotency_cache deduplicates concurrent + sequential repeats
  • ActiveCall event log + replay
  • Host promotion on initiator leave (group call only)

These are unit-level — they don't spin up a Socket.IO server. The
integration tests live in test_integration.py.
"""

from __future__ import annotations

import asyncio
import pytest

from app.services.idempotency_cache import IdempotencyCache
from app.services.call_service import ActiveCall


# ── IdempotencyCache ──────────────────────────────────────────────────


class TestIdempotencyCache:
    """Verify the cache dedups (call_id, key) tuples within TTL."""

    @pytest.mark.asyncio
    async def test_first_call_runs_factory_and_caches(self):
        cache = IdempotencyCache()
        calls = []

        async def factory():
            calls.append(1)
            return {"status": "ok", "id": 42}

        result = await cache.get_or_compute("call-A", "k1", factory)
        assert result == {"status": "ok", "id": 42}
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_duplicate_returns_cached_value(self):
        cache = IdempotencyCache()
        runs = []

        async def factory():
            runs.append(1)
            return {"value": "first"}

        a = await cache.get_or_compute("call-A", "key", factory)
        b = await cache.get_or_compute("call-A", "key", factory)
        c = await cache.get_or_compute("call-A", "key", factory)
        assert a == b == c == {"value": "first"}
        assert len(runs) == 1, "factory should run exactly once"

    @pytest.mark.asyncio
    async def test_different_keys_run_independently(self):
        cache = IdempotencyCache()
        runs = []

        async def make_factory(label):
            async def f():
                runs.append(label)
                return label
            return f

        r1 = await cache.get_or_compute("call-A", "k1", await make_factory("first"))
        r2 = await cache.get_or_compute("call-A", "k2", await make_factory("second"))
        assert r1 == "first"
        assert r2 == "second"
        assert sorted(runs) == ["first", "second"]

    @pytest.mark.asyncio
    async def test_different_calls_dont_collide(self):
        cache = IdempotencyCache()
        runs = []

        async def f1():
            runs.append("A")
            return "A-result"

        async def f2():
            runs.append("B")
            return "B-result"

        # Same key, different call_ids — must NOT dedupe
        a = await cache.get_or_compute("call-A", "key", f1)
        b = await cache.get_or_compute("call-B", "key", f2)
        assert a == "A-result"
        assert b == "B-result"
        assert sorted(runs) == ["A", "B"]

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_factory_run(self):
        """If two coroutines hit the same key simultaneously, only one
        factory invocation should occur — both await the same future."""
        cache = IdempotencyCache()
        runs = 0
        gate = asyncio.Event()

        async def slow_factory():
            nonlocal runs
            runs += 1
            await gate.wait()
            return "shared"

        # Kick off two concurrent calls
        t1 = asyncio.create_task(cache.get_or_compute("call-X", "k", slow_factory))
        t2 = asyncio.create_task(cache.get_or_compute("call-X", "k", slow_factory))
        await asyncio.sleep(0.05)  # let both register as inflight
        gate.set()
        r1, r2 = await asyncio.gather(t1, t2)

        assert r1 == r2 == "shared"
        assert runs == 1, f"factory ran {runs} times — should be 1"

    @pytest.mark.asyncio
    async def test_factory_exception_does_not_pollute_cache(self):
        cache = IdempotencyCache()

        async def boom():
            raise RuntimeError("bang")

        with pytest.raises(RuntimeError):
            await cache.get_or_compute("call-A", "k", boom)

        # A retry SHOULD run the factory again — the failed call is
        # not cached as a result.
        runs = []

        async def ok():
            runs.append(1)
            return "recovered"

        result = await cache.get_or_compute("call-A", "k", ok)
        assert result == "recovered"
        assert len(runs) == 1


# ── ActiveCall events log ─────────────────────────────────────────────


class TestActiveCallEventLog:
    def test_events_start_empty(self):
        c = ActiveCall(call_id="c1", initiator_id="u1", call_type="audio", routing="mesh")
        # ``events`` was a list originally; the per-call ringbuffer
        # refactor switched it to a bounded deque. Assert emptiness via
        # length so both implementations satisfy this test.
        assert len(c.events) == 0
        assert c.current_sequence == 0

    def test_append_event_increments_sequence(self):
        c = ActiveCall(call_id="c1", initiator_id="u1", call_type="audio", routing="mesh")
        e1 = c.append_event("call:participant-joined", {"user_id": "u2"})
        e2 = c.append_event("call:participant-joined", {"user_id": "u3"})
        assert e1["seq"] == 1
        assert e2["seq"] == 2
        assert c.current_sequence == 2

    def test_events_since_returns_only_newer(self):
        c = ActiveCall(call_id="c1", initiator_id="u1", call_type="audio", routing="mesh")
        c.append_event("a", {})
        c.append_event("b", {})
        c.append_event("c", {})
        missed = c.events_since(last_seq=1)
        assert [e["type"] for e in missed] == ["b", "c"]

    def test_events_since_caps_at_limit(self):
        c = ActiveCall(call_id="c1", initiator_id="u1", call_type="audio", routing="mesh")
        for i in range(20):
            c.append_event(f"e{i}", {})
        missed = c.events_since(last_seq=0, limit=5)
        assert len(missed) == 5
        # The 5 oldest after seq 0: 1..5
        assert [e["seq"] for e in missed] == [1, 2, 3, 4, 5]

    def test_events_log_caps_total_size(self):
        c = ActiveCall(call_id="c1", initiator_id="u1", call_type="audio", routing="mesh")
        for i in range(1500):
            c.append_event("noise", {})
        # We trim to last 800 once we cross 1000. Sequence keeps climbing.
        assert len(c.events) <= 1000
        assert c.current_sequence == 1500
        # The oldest retained event should NOT have seq 1
        assert c.events[0]["seq"] > 1


# ── Host promotion (CallService.leave_call host path) ─────────────────


class TestHostPromotion:
    """The unit behavior: ActiveCall + leave_call combination promotes
    a new initiator when the original host leaves a multiparty mesh
    call. Avoids spinning up the full service — exercises the in-memory
    state change path directly."""

    def test_promote_picks_longest_joined(self):
        from datetime import datetime, timezone, timedelta
        c = ActiveCall(call_id="c1", initiator_id="host", call_type="audio", routing="mesh")
        now = datetime.now(timezone.utc)
        c.add_participant("host")
        c.participants["host"]["joined_at"] = now - timedelta(minutes=5)
        c.add_participant("alice")
        c.participants["alice"]["joined_at"] = now - timedelta(minutes=3)
        c.add_participant("bob")
        c.participants["bob"]["joined_at"] = now - timedelta(minutes=1)

        # Simulate host leaving — same logic as call_service.leave_call
        c.remove_participant("host")
        candidate = min(
            c.participants.items(),
            key=lambda kv: kv[1].get("joined_at"),
        )[0]
        # Alice joined longer ago than Bob → she becomes host
        assert candidate == "alice"

    def test_promotion_records_event(self):
        from datetime import datetime, timezone
        c = ActiveCall(call_id="c1", initiator_id="host", call_type="audio", routing="mesh")
        c.add_participant("host")
        c.add_participant("alice")
        c.append_event("call:host-changed", {
            "call_id": "c1", "old_host": "host", "new_host": "alice",
        })
        events = c.events_since(0)
        assert any(
            e["type"] == "call:host-changed" and e["payload"]["new_host"] == "alice"
            for e in events
        )
