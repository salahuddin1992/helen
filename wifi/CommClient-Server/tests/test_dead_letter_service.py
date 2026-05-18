"""
Unit tests for :mod:`app.services.dead_letter_service`.

Covers the pure-logic surface and the DB-backed behaviours:

  * Backoff is monotonic and capped at ``MAX_BACKOFF_SECONDS``.
  * ``record()`` serialises payloads, truncates oversized errors, and
    never raises even on unserializable payload shapes.
  * ``record()`` normalises unknown ``kind`` to ``"unknown"``.
  * ``list_entries`` respects status/kind filters and pagination.
  * ``abandon()`` flips status, sets ``resolved_at`` and stores the note.
  * ``replay_entry()`` transitions to ``replayed`` on success and schedules
    a retry with exponential backoff on failure.
  * ``replay_entry()`` gives up (``abandoned``) after ``MAX_ATTEMPTS``.
  * ``_reaper_tick()`` picks only rows whose ``next_attempt_at`` is due.
  * ``stats()`` returns grouped counts.

The tests monkey-patch ``_dispatch`` to simulate success/failure so we do
not need a running Socket.IO server.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.base import Base
from app.db.session import async_session_factory, engine
from app.models.message_dead_letter import MessageDeadLetter
from app.services import dead_letter_service as dls
from app.services.dead_letter_service import (
    BASE_BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    MAX_BACKOFF_SECONDS,
    DeadLetterService,
    _compute_backoff,
    _truncate_text,
    record,
)


pytestmark = pytest.mark.asyncio


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def module_engine():
    """Ensure the DLQ table exists on the shared module-scoped engine."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine


@pytest.fixture
async def clean_dlq(module_engine):
    """Delete all DLQ rows before each test so state is isolated."""
    from sqlalchemy import delete

    async with async_session_factory() as db:
        await db.execute(delete(MessageDeadLetter))
        await db.commit()
    yield
    async with async_session_factory() as db:
        await db.execute(delete(MessageDeadLetter))
        await db.commit()


# ─────────────────────────────────────────────────────────────────
# Pure-logic helpers
# ─────────────────────────────────────────────────────────────────


def test_backoff_monotonic_and_capped():
    # 30, 60, 120, 240, ... up to MAX_BACKOFF_SECONDS
    d0 = _compute_backoff(0)
    d1 = _compute_backoff(1)
    d2 = _compute_backoff(2)
    assert d0.total_seconds() == BASE_BACKOFF_SECONDS
    assert d1 > d0
    assert d2 > d1
    # Very large attempt_count caps at 1h
    dlarge = _compute_backoff(50)
    assert dlarge.total_seconds() == MAX_BACKOFF_SECONDS


def test_truncate_text_shortens_only_when_needed():
    assert _truncate_text("hello", 10) == "hello"
    assert _truncate_text("hello world", 5) == "he..."
    assert _truncate_text(None, 10) is None
    assert _truncate_text("", 10) == ""


# ─────────────────────────────────────────────────────────────────
# record()
# ─────────────────────────────────────────────────────────────────


async def test_record_persists_row(clean_dlq):
    # FK columns (channel_id/sender_id/message_id) are nullable; leaving
    # them None keeps this unit test self-contained — no parent rows needed.
    row_id = await record(
        kind="fanout",
        reason="unit_test",
        error=RuntimeError("boom"),
        payload={"a": 1, "b": "two"},
    )
    assert row_id is not None

    async with async_session_factory() as db:
        row = await db.get(MessageDeadLetter, row_id)
        assert row is not None
        assert row.kind == "fanout"
        assert row.reason == "unit_test"
        assert row.status == "pending"
        assert row.attempt_count == 0
        # error is formatted as "TypeName: message"
        assert "RuntimeError" in (row.error or "")
        # payload is JSON-encoded
        assert json.loads(row.payload_json) == {"a": 1, "b": "two"}
        # next_attempt_at is set to ~BASE_BACKOFF_SECONDS in the future
        assert row.next_attempt_at is not None


async def test_record_normalises_unknown_kind(clean_dlq):
    row_id = await record(kind="bogus_kind", reason="x")
    assert row_id is not None
    async with async_session_factory() as db:
        row = await db.get(MessageDeadLetter, row_id)
        assert row.kind == "unknown"


async def test_record_handles_unserializable_payload(clean_dlq):
    class Unserializable:
        def __repr__(self):
            return "<Unser>"

    row_id = await record(
        kind="fanout",
        reason="weird",
        payload={"obj": Unserializable()},  # default=str will fall back
    )
    assert row_id is not None
    async with async_session_factory() as db:
        row = await db.get(MessageDeadLetter, row_id)
        # Must not have thrown; payload_json is some string
        assert isinstance(row.payload_json, str) and row.payload_json


async def test_record_truncates_oversized_error(clean_dlq):
    row_id = await record(
        kind="fanout",
        reason="huge",
        error="E" * 5000,
    )
    async with async_session_factory() as db:
        row = await db.get(MessageDeadLetter, row_id)
        assert len(row.error) <= 1024


# ─────────────────────────────────────────────────────────────────
# list_entries / get_entry
# ─────────────────────────────────────────────────────────────────


async def test_list_entries_filters(clean_dlq):
    await record(kind="fanout", reason="a")
    await record(kind="webhook", reason="b")
    await record(kind="fanout", reason="c")

    async with async_session_factory() as db:
        rows, total = await DeadLetterService.list_entries(db, kind="fanout")
        assert total == 2
        assert len(rows) == 2
        for r in rows:
            assert r.kind == "fanout"

        rows_all, total_all = await DeadLetterService.list_entries(db)
        assert total_all == 3


async def test_list_entries_pagination(clean_dlq):
    ids = []
    for i in range(5):
        rid = await record(kind="fanout", reason=f"r-{i}")
        ids.append(rid)

    async with async_session_factory() as db:
        rows1, total = await DeadLetterService.list_entries(db, limit=2, offset=0)
        rows2, _ = await DeadLetterService.list_entries(db, limit=2, offset=2)
        assert total == 5
        assert len(rows1) == 2 and len(rows2) == 2
        # Different rows
        assert {r.id for r in rows1}.isdisjoint({r.id for r in rows2})


# ─────────────────────────────────────────────────────────────────
# abandon
# ─────────────────────────────────────────────────────────────────


async def test_abandon_sets_resolved_at_and_note(clean_dlq):
    row_id = await record(kind="fanout", reason="to-abandon")

    async with async_session_factory() as db:
        row = await DeadLetterService.abandon(db, row_id, note="cannot recover")
        assert row.status == "abandoned"
        assert row.resolved_at is not None
        assert row.operator_note == "cannot recover"


async def test_abandon_missing_returns_none(clean_dlq):
    async with async_session_factory() as db:
        out = await DeadLetterService.abandon(db, "deadbeef" * 4)
        assert out is None


# ─────────────────────────────────────────────────────────────────
# replay_entry
# ─────────────────────────────────────────────────────────────────


async def test_replay_success_marks_replayed(clean_dlq, monkeypatch):
    row_id = await record(
        kind="fanout",
        reason="to-replay",
        payload={"event": "chat:new_message", "channel_id": "ch-1", "message": {"id": "m-1"}},
    )

    async def _fake_dispatch(kind, payload):
        return True

    monkeypatch.setattr(dls, "_dispatch", _fake_dispatch)

    async with async_session_factory() as db:
        row = await DeadLetterService.replay_entry(db, row_id)
        assert row.status == "replayed"
        assert row.resolved_at is not None
        assert row.next_attempt_at is None
        assert row.attempt_count == 1


async def test_replay_failure_schedules_retry(clean_dlq, monkeypatch):
    row_id = await record(kind="fanout", reason="fail-once")

    async def _fake_dispatch(kind, payload):
        return False

    monkeypatch.setattr(dls, "_dispatch", _fake_dispatch)

    async with async_session_factory() as db:
        row = await DeadLetterService.replay_entry(db, row_id)
        assert row.status == "pending"
        assert row.next_attempt_at is not None
        assert row.attempt_count == 1
        # SQLite returns the column as offset-naive despite
        # DateTime(timezone=True) — normalise before comparing.
        na = row.next_attempt_at
        if na.tzinfo is None:
            na = na.replace(tzinfo=timezone.utc)
        assert na > datetime.now(timezone.utc) - timedelta(seconds=1)


async def test_replay_exhausts_after_max_attempts(clean_dlq, monkeypatch):
    row_id = await record(kind="fanout", reason="forever-fail")

    async def _fake_dispatch(kind, payload):
        return False

    monkeypatch.setattr(dls, "_dispatch", _fake_dispatch)

    # Run MAX_ATTEMPTS replay calls — final one must abandon
    for _ in range(MAX_ATTEMPTS):
        async with async_session_factory() as db:
            row = await DeadLetterService.replay_entry(db, row_id)

    assert row.status == "abandoned"
    assert row.attempt_count == MAX_ATTEMPTS
    assert row.resolved_at is not None


async def test_replay_idempotent_on_terminal_status(clean_dlq, monkeypatch):
    row_id = await record(kind="fanout", reason="done")

    async def _fake_dispatch(kind, payload):
        return True

    monkeypatch.setattr(dls, "_dispatch", _fake_dispatch)

    async with async_session_factory() as db:
        r1 = await DeadLetterService.replay_entry(db, row_id)
        assert r1.status == "replayed"
        # A second call must NOT re-dispatch or increment attempts
        prev_attempts = r1.attempt_count
        r2 = await DeadLetterService.replay_entry(db, row_id)
        assert r2.status == "replayed"
        assert r2.attempt_count == prev_attempts


# ─────────────────────────────────────────────────────────────────
# reaper tick
# ─────────────────────────────────────────────────────────────────


async def test_reaper_tick_picks_only_due_rows(clean_dlq, monkeypatch):
    """Only rows whose ``next_attempt_at`` <= now are selected."""
    due_id = await record(kind="fanout", reason="due-now")
    future_id = await record(kind="fanout", reason="not-yet")

    # Manually shove one row into the past and leave the other in the future
    async with async_session_factory() as db:
        due_row = await db.get(MessageDeadLetter, due_id)
        due_row.next_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        future_row = await db.get(MessageDeadLetter, future_id)
        future_row.next_attempt_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.commit()

    calls: list[str] = []

    async def _fake_dispatch(kind, payload):
        calls.append(kind)
        return True

    monkeypatch.setattr(dls, "_dispatch", _fake_dispatch)

    replayed = await DeadLetterService._reaper_tick(batch=10)
    assert replayed == 1
    assert len(calls) == 1

    # Due row now marked replayed
    async with async_session_factory() as db:
        due_after = await db.get(MessageDeadLetter, due_id)
        future_after = await db.get(MessageDeadLetter, future_id)
        assert due_after.status == "replayed"
        assert future_after.status == "pending"


async def test_reaper_start_stop_idempotent():
    # start() twice is safe; stop() cleans up
    await DeadLetterService.start()
    await DeadLetterService.start()
    assert DeadLetterService._reaper_task is not None
    await DeadLetterService.stop()
    assert DeadLetterService._reaper_task is None
    # Calling stop again on an already-stopped reaper must not raise
    await DeadLetterService.stop()


# ─────────────────────────────────────────────────────────────────
# stats
# ─────────────────────────────────────────────────────────────────


async def test_stats_groups_by_status_and_kind(clean_dlq):
    await record(kind="fanout", reason="a")
    await record(kind="webhook", reason="b")
    w_id = await record(kind="webhook", reason="c")

    # Manually abandon one to exercise multiple statuses
    async with async_session_factory() as db:
        await DeadLetterService.abandon(db, w_id)

    async with async_session_factory() as db:
        stats = await DeadLetterService.stats(db)
        assert stats["by_kind"].get("fanout", 0) == 1
        assert stats["by_kind"].get("webhook", 0) == 2
        assert stats["by_status"].get("pending", 0) == 2
        assert stats["by_status"].get("abandoned", 0) == 1
        assert stats["oldest_pending_at"] is not None
