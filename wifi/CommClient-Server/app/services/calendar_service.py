"""
Helen calendar — internal events, recurring meetings, reminders.

Why
---
Helen already does video calls. Adding a calendar lets users pre-book
those calls, see "what does Bob have today?", get reminders 5 minutes
before a meeting, and export an .ics file the OS calendar app can
subscribe to over LAN HTTP.

What this is, what it isn't
---------------------------
* IS: a thin event store + reminder scheduler + RFC 5545 .ics export.
* IS NOT: a full Outlook replacement. No room booking, no
  travel-time computation, no per-user availability cross-checks.
  Those can come later if anyone needs them.

Storage
-------
Single SQLite table; per-user and per-channel indices for the
hot-path queries.

Wire shape
----------
  POST /api/calendar/events           create
  GET  /api/calendar/events?range=…   list
  PATCH /api/calendar/events/{id}     edit
  DELETE /api/calendar/events/{id}    cancel
  GET  /api/calendar/feed.ics         per-user iCal feed (read-only)
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional


# ── Data model ─────────────────────────────────────────────────────


@dataclass
class CalendarEvent:
    event_id: str
    creator_id: str
    title: str
    start_at: float            # unix epoch
    end_at: float
    description: str = ""
    location: str = ""               # channel name or physical room
    channel_id: Optional[str] = None
    attendees: list[str] = field(default_factory=list)
    recurrence: Optional[str] = None   # RFC 5545 RRULE string
    reminders: list[int] = field(default_factory=lambda: [5, 30])
    # ↑ minutes before start_at to fire a reminder
    created_at: float = field(default_factory=time.time)
    cancelled: bool = False


# ── Store ──────────────────────────────────────────────────────────


class CalendarStore:

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with sqlite3.connect(db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS calendar_events (
                    event_id    TEXT PRIMARY KEY,
                    creator_id  TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    start_at    REAL NOT NULL,
                    end_at      REAL NOT NULL,
                    description TEXT,
                    location    TEXT,
                    channel_id  TEXT,
                    attendees_json   TEXT,
                    recurrence  TEXT,
                    reminders_json   TEXT,
                    created_at  REAL NOT NULL,
                    cancelled   INTEGER NOT NULL DEFAULT 0
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_cal_creator "
                       "ON calendar_events(creator_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cal_start "
                       "ON calendar_events(start_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cal_channel "
                       "ON calendar_events(channel_id)")

    def create(self, event: CalendarEvent) -> CalendarEvent:
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO calendar_events VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id, event.creator_id, event.title,
                    event.start_at, event.end_at, event.description,
                    event.location, event.channel_id,
                    json.dumps(event.attendees, ensure_ascii=False),
                    event.recurrence,
                    json.dumps(event.reminders),
                    event.created_at, int(event.cancelled),
                ),
            )
        return event

    def get(self, event_id: str) -> Optional[CalendarEvent]:
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT * FROM calendar_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return self._row_to_event(row) if row else None

    def cancel(self, event_id: str) -> bool:
        with sqlite3.connect(self.db_path) as c:
            cur = c.execute(
                "UPDATE calendar_events SET cancelled=1 "
                "WHERE event_id=?", (event_id,),
            )
            return cur.rowcount > 0

    def list_for_user(self, user_id: str,
                       start: Optional[float] = None,
                       end: Optional[float] = None,
                       limit: int = 200) -> list[CalendarEvent]:
        sql = (
            "SELECT * FROM calendar_events "
            "WHERE (creator_id=? OR attendees_json LIKE ?)"
        )
        params: list = [user_id, f'%"{user_id}"%']
        if start is not None:
            sql += " AND end_at >= ?"; params.append(start)
        if end is not None:
            sql += " AND start_at <= ?"; params.append(end)
        sql += " AND cancelled=0 ORDER BY start_at ASC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(sql, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def upcoming_reminders(self, now: float,
                             window_sec: float = 60.0,
                             ) -> Iterator[tuple[CalendarEvent, int]]:
        """Yield ``(event, minutes_before)`` tuples for every event
        whose reminder ``minutes_before`` matches the current minute
        within ``window_sec``. Call this once a minute from a worker."""
        sql = ("SELECT * FROM calendar_events WHERE cancelled=0 "
                "AND start_at > ? AND start_at < ?")
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                sql,
                (now, now + 24 * 3600),  # only next 24h
            ).fetchall()
        for row in rows:
            evt = self._row_to_event(row)
            for m in evt.reminders:
                fire_at = evt.start_at - (m * 60)
                if abs(fire_at - now) <= window_sec / 2:
                    yield evt, m

    def _row_to_event(self, row: tuple) -> CalendarEvent:
        return CalendarEvent(
            event_id=row[0], creator_id=row[1], title=row[2],
            start_at=row[3], end_at=row[4],
            description=row[5] or "", location=row[6] or "",
            channel_id=row[7],
            attendees=json.loads(row[8]) if row[8] else [],
            recurrence=row[9],
            reminders=json.loads(row[10]) if row[10] else [],
            created_at=row[11], cancelled=bool(row[12]),
        )


# ── ICS export ─────────────────────────────────────────────────────


def export_ics(events: list[CalendarEvent],
                calname: str = "Helen") -> str:
    """RFC 5545 minimal-but-valid calendar feed for any OS calendar
    that subscribes to a HTTP URL."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Helen Project//Helen Calendar//EN",
        f"X-WR-CALNAME:{calname}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for e in events:
        if e.cancelled:
            continue
        dt_start = _to_ics_utc(e.start_at)
        dt_end = _to_ics_utc(e.end_at)
        dt_stamp = _to_ics_utc(e.created_at)
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{e.event_id}@helen.lan")
        lines.append(f"DTSTAMP:{dt_stamp}")
        lines.append(f"DTSTART:{dt_start}")
        lines.append(f"DTEND:{dt_end}")
        lines.append(f"SUMMARY:{_ics_escape(e.title)}")
        if e.description:
            lines.append(f"DESCRIPTION:{_ics_escape(e.description)}")
        if e.location:
            lines.append(f"LOCATION:{_ics_escape(e.location)}")
        if e.recurrence:
            lines.append(f"RRULE:{e.recurrence}")
        for att in e.attendees:
            lines.append(f"ATTENDEE;CN={_ics_escape(att)}:mailto:{att}@helen.lan")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _to_ics_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )


def _ics_escape(s: str) -> str:
    return (s.replace("\\", "\\\\")
              .replace(",", "\\,")
              .replace(";", "\\;")
              .replace("\n", "\\n"))


# ── Reminder worker ────────────────────────────────────────────────


class ReminderWorker:
    """Polls the calendar every minute and fires reminders via the
    push manager. Wire ``push_manager.push`` as the callback at
    construction time."""

    def __init__(
        self,
        store: CalendarStore,
        push: callable,                   # (user_id, payload) → coroutine
    ) -> None:
        self.store = store
        self.push = push
        self._task: Optional[asyncio.Task] = None
        self._fired: set[tuple[str, int]] = set()
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(),
                                                 name="calendar-reminder")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                    return
                except asyncio.TimeoutError:
                    pass
                now = time.time()
                for evt, mins in self.store.upcoming_reminders(now):
                    key = (evt.event_id, mins)
                    if key in self._fired:
                        continue
                    self._fired.add(key)
                    payload = {
                        "type": "calendar.reminder",
                        "event_id": evt.event_id,
                        "title": evt.title,
                        "starts_in_minutes": mins,
                        "start_at": evt.start_at,
                    }
                    targets = list(set([evt.creator_id]
                                         + list(evt.attendees)))
                    for uid in targets:
                        try:
                            result = self.push(uid, payload)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception:
                            continue
        except asyncio.CancelledError:
            return


def new_event_id() -> str:
    return secrets.token_urlsafe(8)
