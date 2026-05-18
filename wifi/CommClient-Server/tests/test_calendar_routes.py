"""
Tests for /api/calendar/* — the in-LAN event store + ICS feed.
"""

from __future__ import annotations

import pytest
import time


@pytest.mark.asyncio
async def test_create_event_persists_and_lists(client, auth_headers):
    now = int(time.time())
    payload = {
        "title": "Standup",
        "start_at": now + 600,
        "end_at": now + 1800,
        "description": "Daily standup",
        "location": "Channel #general",
    }
    r = await client.post("/api/calendar/events", json=payload, headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Standup"
    event_id = body["event_id"]

    listing = await client.get("/api/calendar/events", headers=auth_headers)
    assert listing.status_code == 200
    titles = [e["title"] for e in listing.json()["events"]]
    assert "Standup" in titles
    ids = [e["event_id"] for e in listing.json()["events"]]
    assert event_id in ids


@pytest.mark.asyncio
async def test_create_rejects_inverted_window(client, auth_headers):
    now = int(time.time())
    r = await client.post(
        "/api/calendar/events",
        json={"title": "Bad", "start_at": now + 1000, "end_at": now + 500},
        headers=auth_headers,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_edit_only_creator(client, auth_headers, second_user_headers):
    now = int(time.time())
    r = await client.post(
        "/api/calendar/events",
        json={"title": "Mine", "start_at": now + 100, "end_at": now + 200},
        headers=auth_headers,
    )
    eid = r.json()["event_id"]

    # Second user trying to edit — must be 403
    bad = await client.patch(
        f"/api/calendar/events/{eid}",
        json={"title": "Hijacked"},
        headers=second_user_headers,
    )
    assert bad.status_code == 403


@pytest.mark.asyncio
async def test_cancel_marks_event(client, auth_headers):
    now = int(time.time())
    r = await client.post(
        "/api/calendar/events",
        json={"title": "Doomed", "start_at": now + 100, "end_at": now + 200},
        headers=auth_headers,
    )
    eid = r.json()["event_id"]
    rd = await client.delete(f"/api/calendar/events/{eid}", headers=auth_headers)
    assert rd.status_code == 200
    assert rd.json()["cancelled"] is True

    # Cancelled events shouldn't appear in list
    listing = await client.get("/api/calendar/events", headers=auth_headers)
    ids = [e["event_id"] for e in listing.json()["events"]]
    assert eid not in ids


@pytest.mark.asyncio
async def test_ics_feed_returns_text_calendar(client, auth_headers):
    now = int(time.time())
    await client.post(
        "/api/calendar/events",
        json={"title": "ICS", "start_at": now + 100, "end_at": now + 200},
        headers=auth_headers,
    )
    r = await client.get("/api/calendar/feed.ics", headers=auth_headers)
    assert r.status_code == 200
    assert "text/calendar" in r.headers.get("content-type", "")
    assert "BEGIN:VCALENDAR" in r.text
    assert "BEGIN:VEVENT" in r.text
    assert "ICS" in r.text


@pytest.mark.asyncio
async def test_unauthenticated_blocked(client):
    r = await client.get("/api/calendar/events")
    assert r.status_code == 403
