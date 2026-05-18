"""
Calendar REST endpoints — events, list, edit, cancel, ICS feed.

Backed by app.services.calendar_service.CalendarStore (SQLite). Reminders
are emitted via the ReminderWorker started in main.py lifespan.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.deps import get_current_user_id
from app.core.logging import get_logger
from app.services.calendar_service import (
    CalendarEvent,
    CalendarStore,
    export_ics,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/calendar", tags=["calendar"])

_settings = get_settings()
_STORE: Optional[CalendarStore] = None


def _get_store() -> CalendarStore:
    """Lazy singleton — DB sits in same dir as the SQLite file."""
    global _STORE
    if _STORE is None:
        sqlite_p = Path(_settings.SQLITE_PATH)
        base_dir = sqlite_p.resolve().parent if sqlite_p.is_absolute() \
            else (_settings.PROJECT_ROOT / sqlite_p).resolve().parent
        base_dir.mkdir(parents=True, exist_ok=True)
        _STORE = CalendarStore(str(base_dir / "calendar.db"))
    return _STORE


class EventCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    start_at: float
    end_at: float
    description: str = ""
    location: str = ""
    channel_id: Optional[str] = None
    attendees: list[str] = Field(default_factory=list)
    recurrence: Optional[str] = None
    reminders: list[int] = Field(default_factory=lambda: [5, 30])


class EventPatch(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    start_at: Optional[float] = None
    end_at: Optional[float] = None
    description: Optional[str] = None
    location: Optional[str] = None
    channel_id: Optional[str] = None
    attendees: Optional[list[str]] = None
    recurrence: Optional[str] = None
    reminders: Optional[list[int]] = None


def _event_to_dict(e: CalendarEvent) -> dict:
    return {
        "event_id": e.event_id,
        "creator_id": e.creator_id,
        "title": e.title,
        "start_at": e.start_at,
        "end_at": e.end_at,
        "description": e.description,
        "location": e.location,
        "channel_id": e.channel_id,
        "attendees": e.attendees,
        "recurrence": e.recurrence,
        "reminders": e.reminders,
        "created_at": e.created_at,
        "cancelled": e.cancelled,
    }


@router.post("/events")
async def create_event(
    payload: EventCreate,
    user_id: str = Depends(get_current_user_id),
):
    if payload.end_at <= payload.start_at:
        raise HTTPException(status_code=400, detail="end_at must be > start_at")
    evt = CalendarEvent(
        event_id=secrets.token_urlsafe(10),
        creator_id=user_id,
        title=payload.title,
        start_at=payload.start_at,
        end_at=payload.end_at,
        description=payload.description,
        location=payload.location,
        channel_id=payload.channel_id,
        attendees=payload.attendees,
        recurrence=payload.recurrence,
        reminders=payload.reminders or [5, 30],
    )
    _get_store().create(evt)
    logger.info("calendar_event_created",
                event_id=evt.event_id, creator=user_id)
    return _event_to_dict(evt)


@router.get("/events")
async def list_events(
    start: Optional[float] = Query(None),
    end: Optional[float] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    user_id: str = Depends(get_current_user_id),
):
    if start is None:
        start = time.time() - 86400
    if end is None:
        end = time.time() + 30 * 86400
    events = _get_store().list_for_user(
        user_id=user_id, start=start, end=end, limit=limit,
    )
    return {"events": [_event_to_dict(e) for e in events]}


@router.get("/events/{event_id}")
async def get_event(
    event_id: str,
    user_id: str = Depends(get_current_user_id),
):
    evt = _get_store().get(event_id)
    if not evt or evt.cancelled:
        raise HTTPException(status_code=404, detail="Event not found")
    if user_id != evt.creator_id and user_id not in evt.attendees:
        raise HTTPException(status_code=403, detail="Not your event")
    return _event_to_dict(evt)


@router.patch("/events/{event_id}")
async def edit_event(
    event_id: str,
    payload: EventPatch,
    user_id: str = Depends(get_current_user_id),
):
    store = _get_store()
    evt = store.get(event_id)
    if not evt or evt.cancelled:
        raise HTTPException(status_code=404, detail="Event not found")
    if evt.creator_id != user_id:
        raise HTTPException(status_code=403, detail="Only creator can edit")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(evt, k, v)
    if evt.end_at <= evt.start_at:
        raise HTTPException(status_code=400, detail="end_at must be > start_at")
    store.create(evt)   # INSERT OR REPLACE — same primary key.
    logger.info("calendar_event_edited", event_id=event_id, by=user_id)
    return _event_to_dict(evt)


@router.delete("/events/{event_id}")
async def cancel_event(
    event_id: str,
    user_id: str = Depends(get_current_user_id),
):
    store = _get_store()
    evt = store.get(event_id)
    if not evt:
        raise HTTPException(status_code=404, detail="Event not found")
    if evt.creator_id != user_id:
        raise HTTPException(status_code=403, detail="Only creator can cancel")
    store.cancel(event_id)
    logger.info("calendar_event_cancelled", event_id=event_id, by=user_id)
    return {"ok": True, "event_id": event_id, "cancelled": True}


@router.get("/feed.ics", response_class=PlainTextResponse)
async def ics_feed(
    user_id: str = Depends(get_current_user_id),
):
    """Per-user iCal feed; subscribe with any RFC 5545 client over LAN."""
    events = _get_store().list_for_user(
        user_id=user_id,
        start=time.time() - 30 * 86400,
        end=time.time() + 365 * 86400,
        limit=1000,
    )
    body = export_ics(events, calname=f"Helen — {user_id}")
    return PlainTextResponse(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="helen.ics"'},
    )
