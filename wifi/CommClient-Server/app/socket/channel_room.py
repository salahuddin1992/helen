"""
Lazy channel-room population.

Pre-joining every user to every channel room on socket-connect costs one SELECT
per connect, which quadruples connect latency under mass-join bursts. Instead,
rooms are populated on first use: when a broadcast first targets
``channel:{id}``, we enumerate the channel's members, look up every online sid,
and enter_room them all in parallel. Subsequent broadcasts hit the warm room
and are O(1) from the handler's perspective.

Mid-session joiners (users who enter a channel after their socket is already
live) are handled by :func:`add_user_to_channel_room`, which the channel
membership service can invoke explicitly.
"""

from __future__ import annotations

import asyncio
from typing import Iterable

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.channel import ChannelMember
from app.services.presence_service import presence_service

logger = get_logger(__name__)


_populated: set[str] = set()
_populate_locks: dict[str, asyncio.Lock] = {}


def room_name(channel_id: str) -> str:
    return f"channel:{channel_id}"


async def ensure_populated(sio, channel_id: str) -> None:
    """Put every currently-online member of ``channel_id`` into its room.

    Idempotent — re-entries are cheap (set check + short-circuit).
    """
    if channel_id in _populated:
        return
    lock = _populate_locks.setdefault(channel_id, asyncio.Lock())
    async with lock:
        if channel_id in _populated:
            return
        try:
            async with async_session_factory() as db:
                rows = (await db.execute(
                    select(ChannelMember.user_id).where(
                        ChannelMember.channel_id == channel_id
                    )
                )).all()
            member_ids: list[str] = [r[0] for r in rows]
            sids: list[str] = []
            for uid in member_ids:
                sids.extend(presence_service.get_sids(uid) or [])
            if sids:
                room = room_name(channel_id)
                # enter_room is synchronous under the hood (dict mutation via
                # the socket.io manager). Call it directly instead of gathering
                # N coroutines — at N=10k that's 10k Task objects created just
                # to await a synchronous dict add, which has its own overhead.
                for s in sids:
                    try:
                        await sio.enter_room(s, room)
                    except Exception:
                        pass
            _populated.add(channel_id)
            logger.info(
                "channel_room_populated",
                channel_id=channel_id,
                members=len(member_ids),
                online_sids=len(sids),
            )
        except Exception as exc:
            logger.warning(
                "channel_room_populate_failed",
                channel_id=channel_id,
                error=str(exc),
            )


async def add_user_to_channel_room(sio, channel_id: str, user_id: str) -> None:
    """Add a user's live sids to a populated room (mid-session join)."""
    if channel_id not in _populated:
        return  # Room not warm yet — will be seeded on next broadcast.
    sids = list(presence_service.get_sids(user_id) or [])
    if not sids:
        return
    room = room_name(channel_id)
    await asyncio.gather(
        *(sio.enter_room(s, room) for s in sids),
        return_exceptions=True,
    )


def forget_channel(channel_id: str) -> None:
    """Drop the populated flag — next broadcast rebuilds the membership set."""
    _populated.discard(channel_id)
    _populate_locks.pop(channel_id, None)


async def add_new_sid(sio, user_id: str, sid: str) -> None:
    """When a user's new sid comes online, slip it into every warm channel room
    that the user belongs to. Cheaper than re-populating the whole room.
    """
    if not _populated:
        return
    try:
        async with async_session_factory() as db:
            rows = (await db.execute(
                select(ChannelMember.channel_id).where(
                    ChannelMember.user_id == user_id
                )
            )).all()
    except Exception as exc:
        logger.warning("channel_new_sid_lookup_failed", user_id=user_id, error=str(exc))
        return
    warm = [cid for (cid,) in rows if cid in _populated]
    if not warm:
        return
    await asyncio.gather(
        *(sio.enter_room(sid, room_name(cid)) for cid in warm),
        return_exceptions=True,
    )
