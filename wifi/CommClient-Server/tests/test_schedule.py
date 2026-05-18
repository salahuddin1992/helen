"""
Tests for user availability schedules + away messages (task #68).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.models.user import User
from app.services.schedule_service import ScheduleService


async def _make_user(db, username: str) -> User:
    user = User(
        username=username,
        display_name=username.capitalize(),
        password_hash="x",
        status="online",
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


# ─────────────────────────────────────────────────────────
# Rules CRUD
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_rule(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    rec = await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=540, end_minute=1020)
    assert rec.weekday == 0
    assert rec.start_minute == 540
    assert rec.end_minute == 1020
    assert rec.status == "available"


@pytest.mark.asyncio
async def test_add_rule_validates_window(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    with pytest.raises(ValidationError):
        await ScheduleService.add_rule(db_session, user.id, weekday=7, start_minute=0, end_minute=60)
    with pytest.raises(ValidationError):
        await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=600, end_minute=600)
    with pytest.raises(ValidationError):
        await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=600, end_minute=300)


@pytest.mark.asyncio
async def test_list_rules_sorted(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    await ScheduleService.add_rule(db_session, user.id, weekday=2, start_minute=540, end_minute=1020)
    await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=540, end_minute=1020)
    items = await ScheduleService.list_rules(db_session, user.id)
    assert [r.weekday for r in items] == [0, 2]


@pytest.mark.asyncio
async def test_update_rule(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    rec = await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=540, end_minute=1020)
    upd = await ScheduleService.update_rule(
        db_session, rec.id, user.id, end_minute=1080, status="busy"
    )
    assert upd.end_minute == 1080
    assert upd.status == "busy"


@pytest.mark.asyncio
async def test_delete_rule(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    rec = await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=540, end_minute=1020)
    removed = await ScheduleService.delete_rule(db_session, rec.id, user.id)
    assert removed is True
    again = await ScheduleService.delete_rule(db_session, rec.id, user.id)
    assert again is False


@pytest.mark.asyncio
async def test_other_user_cannot_modify(db_session):
    a = await _make_user(db_session, "alice")
    b = await _make_user(db_session, "bob")
    await db_session.commit()
    rec = await ScheduleService.add_rule(db_session, a.id, weekday=0, start_minute=540, end_minute=1020)
    with pytest.raises(NotFoundError):
        await ScheduleService.update_rule(db_session, rec.id, b.id, status="busy")


# ─────────────────────────────────────────────────────────
# Away messages
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_and_get_away(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    rec = await ScheduleService.set_away_message(db_session, user.id, "On vacation")
    assert rec.text == "On vacation"
    assert rec.is_active is True
    fetched = await ScheduleService.get_away_message(db_session, user.id)
    assert fetched.id == rec.id


@pytest.mark.asyncio
async def test_set_away_idempotent_update(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    a = await ScheduleService.set_away_message(db_session, user.id, "first")
    b = await ScheduleService.set_away_message(db_session, user.id, "second")
    assert a.id == b.id
    assert b.text == "second"


@pytest.mark.asyncio
async def test_clear_away(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    await ScheduleService.set_away_message(db_session, user.id, "x")
    removed = await ScheduleService.clear_away_message(db_session, user.id)
    assert removed is True
    assert await ScheduleService.get_away_message(db_session, user.id) is None


@pytest.mark.asyncio
async def test_set_away_validates_mode(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    with pytest.raises(ValidationError):
        await ScheduleService.set_away_message(db_session, user.id, "x", mode="garbage")


# ─────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_rules_means_always_available(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    assert await ScheduleService.is_available(db_session, user.id) is True


@pytest.mark.asyncio
async def test_inside_window_is_available(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    # Mon 09:00-17:00
    await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=540, end_minute=1020)

    inside = datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc)  # Mon
    assert await ScheduleService.is_available(db_session, user.id, at=inside) is True


@pytest.mark.asyncio
async def test_outside_window_not_available(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=540, end_minute=1020)

    outside = datetime(2026, 4, 6, 20, 0, tzinfo=timezone.utc)  # Mon 20:00
    assert await ScheduleService.is_available(db_session, user.id, at=outside) is False


@pytest.mark.asyncio
async def test_always_on_overrides_schedule(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=540, end_minute=1020)
    await ScheduleService.set_away_message(db_session, user.id, "ignore me", mode="always_on")

    outside = datetime(2026, 4, 6, 20, 0, tzinfo=timezone.utc)
    assert await ScheduleService.is_available(db_session, user.id, at=outside) is True


@pytest.mark.asyncio
async def test_always_away_overrides_schedule(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=540, end_minute=1020)
    await ScheduleService.set_away_message(db_session, user.id, "out", mode="always_away")

    inside = datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc)
    assert await ScheduleService.is_available(db_session, user.id, at=inside) is False


@pytest.mark.asyncio
async def test_resolve_status_returns_away_text(db_session):
    user = await _make_user(db_session, "alice")
    await db_session.commit()
    await ScheduleService.add_rule(db_session, user.id, weekday=0, start_minute=540, end_minute=1020)
    await ScheduleService.set_away_message(db_session, user.id, "see you tomorrow")

    outside = datetime(2026, 4, 6, 20, 0, tzinfo=timezone.utc)
    snap = await ScheduleService.resolve_status(db_session, user.id, at=outside)
    assert snap["available"] is False
    assert snap["away_text"] == "see you tomorrow"
