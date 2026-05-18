"""
Dead-Letter Queue (DLQ) service for messaging side effects.

Public API
----------
  * :func:`record` — persist a failed operation. Safe to call from any
    exception handler; never raises.
  * :func:`DeadLetterService.list_entries` — paginated admin listing.
  * :func:`DeadLetterService.get_entry` — single-row detail.
  * :func:`DeadLetterService.replay_entry` — re-dispatch the captured
    side effect and update the row's lifecycle.
  * :func:`DeadLetterService.abandon` — mark a row abandoned.
  * :func:`DeadLetterService.reaper_loop` — periodic task that picks up
    rows whose ``next_attempt_at`` has passed and replays them with
    exponential backoff (capped at :attr:`MAX_ATTEMPTS`).

Supported ``kind`` values and replay semantics
---------------------------------------------
  * ``fanout`` — re-emit the captured chat payload over Socket.IO to
    every channel member.
  * ``webhook`` — re-queue the webhook via
    :func:`WebhookService.emit`.
  * ``push``   — re-dispatch a :class:`PushPayload` via
    :func:`push_dispatcher.dispatch_bulk`.
  * ``scheduled`` / ``notification`` / ``sfu_event`` / ``unknown`` —
    stored but replay is a no-op (admin visibility only).

Design constraints
------------------
  * All writes run in short transactions via a dedicated session so we
    never hold a DB session across multi-step replay work.
  * Backoff: ``2^attempt * 30s`` up to ``MAX_BACKOFF_SECONDS`` = 1h.
  * After ``MAX_ATTEMPTS`` (default 8) the row is marked ``abandoned``.
  * The reaper loop is idempotent — safe to call ``start()`` multiple
    times; only one task is ever scheduled.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.message_dead_letter import MessageDeadLetter

logger = get_logger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────────────

MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600  # 1 hour cap
REAPER_TICK_SECONDS = 60
MAX_PAYLOAD_BYTES = 64 * 1024  # Truncate absurdly large payloads
MAX_ERROR_BYTES = 1024

SUPPORTED_KINDS = {
    "fanout",
    "webhook",
    "push",
    "scheduled",
    "notification",
    "sfu_event",
    # Federation RPC forward failed — peer was unreachable or returned
    # 5xx. Replay is currently a no-op (admin visibility only); a
    # production deployment should add a replay handler that re-issues
    # the signed POST when the peer comes back into circuit.
    "federation_rpc",
    # Federation event emit failed (cross-server socket event delivery).
    # Same shape as fanout but originates from emit_to_remote_user.
    "federation_emit",
    "unknown",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compute_backoff(attempt_count: int) -> timedelta:
    # attempt_count starts at 0 on first failure
    delay = min(
        BASE_BACKOFF_SECONDS * (2 ** max(0, attempt_count)),
        MAX_BACKOFF_SECONDS,
    )
    return timedelta(seconds=delay)


def _truncate_text(s: str | None, limit: int) -> str | None:
    if not s:
        return s
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


# ── Recording ────────────────────────────────────────────────────────────────


async def record(
    *,
    kind: str,
    reason: str,
    error: Exception | str | None = None,
    payload: dict[str, Any] | None = None,
    message_id: str | None = None,
    channel_id: str | None = None,
    sender_id: str | None = None,
) -> str | None:
    """
    Persist a failed side-effect. Returns the DLQ row ID or ``None`` on
    failure.

    Never raises — the caller's hot path must stay clean.
    """
    try:
        if kind not in SUPPORTED_KINDS:
            kind = "unknown"
        err_text: str | None
        if isinstance(error, Exception):
            err_text = f"{type(error).__name__}: {error}"
        else:
            err_text = error  # type: ignore[assignment]
        err_text = _truncate_text(err_text, MAX_ERROR_BYTES)

        payload_json: str | None = None
        if payload is not None:
            try:
                payload_json = json.dumps(payload, default=str)
            except Exception:
                payload_json = json.dumps({"unserializable": str(payload)[:1024]})
            if payload_json and len(payload_json) > MAX_PAYLOAD_BYTES:
                payload_json = payload_json[: MAX_PAYLOAD_BYTES - 16] + '"...TRUNCATED"'

        async with async_session_factory() as db:
            row = MessageDeadLetter(
                message_id=message_id,
                channel_id=channel_id,
                sender_id=sender_id,
                kind=kind,
                reason=reason,
                error=err_text,
                payload_json=payload_json,
                status="pending",
                attempt_count=0,
                next_attempt_at=_now() + _compute_backoff(0),
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            logger.warning(
                "dlq_recorded",
                id=row.id,
                kind=kind,
                reason=reason,
                message_id=message_id,
                channel_id=channel_id,
            )
            return row.id
    except Exception as e:
        logger.error("dlq_record_failed", error=str(e), kind=kind, reason=reason)
        return None


# ── Listing / retrieval ──────────────────────────────────────────────────────


class DeadLetterService:
    """Admin-level CRUD + replay operations for the DLQ."""

    @staticmethod
    async def list_entries(
        db: AsyncSession,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[MessageDeadLetter], int]:
        from sqlalchemy import func

        base = select(MessageDeadLetter)
        if status:
            base = base.where(MessageDeadLetter.status == status)
        if kind:
            base = base.where(MessageDeadLetter.kind == kind)

        total = (
            await db.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0

        rows = (
            await db.execute(
                base.order_by(MessageDeadLetter.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        return list(rows), int(total)

    @staticmethod
    async def get_entry(
        db: AsyncSession, entry_id: str
    ) -> Optional[MessageDeadLetter]:
        return await db.get(MessageDeadLetter, entry_id)

    @staticmethod
    async def abandon(
        db: AsyncSession, entry_id: str, note: str | None = None
    ) -> Optional[MessageDeadLetter]:
        row = await db.get(MessageDeadLetter, entry_id)
        if not row:
            return None
        row.status = "abandoned"
        row.resolved_at = _now()
        if note:
            row.operator_note = _truncate_text(note, 2048)
        await db.commit()
        await db.refresh(row)
        logger.info("dlq_abandoned", id=entry_id)
        return row

    @staticmethod
    async def stats(db: AsyncSession) -> dict[str, Any]:
        from sqlalchemy import func

        counts_by_status = dict(
            (
                await db.execute(
                    select(MessageDeadLetter.status, func.count())
                    .group_by(MessageDeadLetter.status)
                )
            ).all()
        )
        counts_by_kind = dict(
            (
                await db.execute(
                    select(MessageDeadLetter.kind, func.count())
                    .group_by(MessageDeadLetter.kind)
                )
            ).all()
        )
        oldest_pending = (
            await db.execute(
                select(MessageDeadLetter.created_at)
                .where(MessageDeadLetter.status == "pending")
                .order_by(MessageDeadLetter.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return {
            "by_status": counts_by_status,
            "by_kind": counts_by_kind,
            "oldest_pending_at": (
                oldest_pending.isoformat() if oldest_pending else None
            ),
        }

    # ── Replay ──────────────────────────────────────────────────────────────

    @staticmethod
    async def replay_entry(
        db: AsyncSession, entry_id: str
    ) -> Optional[MessageDeadLetter]:
        """
        Replay a DLQ entry on demand. Updates the row with the outcome.

        Returns the updated row (or ``None`` if not found).
        """
        row = await db.get(MessageDeadLetter, entry_id)
        if not row:
            return None
        if row.status in ("replayed", "abandoned"):
            return row

        row.status = "replaying"
        row.last_attempt_at = _now()
        row.attempt_count += 1
        await db.commit()

        try:
            payload = json.loads(row.payload_json) if row.payload_json else {}
        except Exception:
            payload = {}

        ok = await _dispatch(row.kind, payload)

        # Reload with fresh session state
        row = await db.get(MessageDeadLetter, entry_id)
        assert row is not None

        if ok:
            row.status = "replayed"
            row.resolved_at = _now()
            row.next_attempt_at = None
            row.error = None
            logger.info("dlq_replayed", id=entry_id, kind=row.kind)
        else:
            if row.attempt_count >= MAX_ATTEMPTS:
                row.status = "abandoned"
                row.resolved_at = _now()
                row.next_attempt_at = None
                logger.warning(
                    "dlq_abandoned_max_attempts",
                    id=entry_id,
                    kind=row.kind,
                )
            else:
                row.status = "pending"
                row.next_attempt_at = _now() + _compute_backoff(row.attempt_count)
                logger.info(
                    "dlq_replay_failed",
                    id=entry_id,
                    kind=row.kind,
                    next_attempt_at=row.next_attempt_at.isoformat(),
                )
        await db.commit()
        await db.refresh(row)
        return row

    # ── Reaper ──────────────────────────────────────────────────────────────

    _reaper_task: asyncio.Task | None = None

    @classmethod
    async def start(cls) -> None:
        if cls._reaper_task is None or cls._reaper_task.done():
            cls._reaper_task = asyncio.create_task(cls._reaper_loop())
            logger.info("dlq_reaper_started")

    @classmethod
    async def stop(cls) -> None:
        if cls._reaper_task:
            cls._reaper_task.cancel()
            try:
                await cls._reaper_task
            except asyncio.CancelledError:
                pass
            cls._reaper_task = None
            logger.info("dlq_reaper_stopped")

    @classmethod
    async def _reaper_loop(cls) -> None:
        while True:
            try:
                await asyncio.sleep(REAPER_TICK_SECONDS)
                await cls._reaper_tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("dlq_reaper_error", error=str(e))

    # Per-tick parallelism — too high and we starve the broker / DB,
    # too low and a backlog of 1000 entries takes forever to drain.
    REAPER_CONCURRENCY = 8

    @classmethod
    async def _reaper_tick(cls, *, batch: int = 50) -> int:
        """One pass over due rows. Returns number replayed."""
        now = _now()
        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    select(MessageDeadLetter)
                    .where(
                        MessageDeadLetter.status == "pending",
                        MessageDeadLetter.next_attempt_at <= now,
                    )
                    .order_by(MessageDeadLetter.next_attempt_at.asc())
                    .limit(batch)
                )
            ).scalars().all()

        if not rows:
            return 0

        sem = asyncio.Semaphore(cls.REAPER_CONCURRENCY)

        async def _one(entry_id: str) -> bool:
            async with sem:
                async with async_session_factory() as db:
                    out = await cls.replay_entry(db, entry_id)
                    return bool(out and out.status == "replayed")

        results = await asyncio.gather(
            *(_one(r.id) for r in rows), return_exceptions=True,
        )
        replayed = sum(1 for r in results if r is True)
        logger.info(
            "dlq_reaper_tick",
            picked=len(rows),
            replayed=replayed,
            concurrency=cls.REAPER_CONCURRENCY,
        )
        return replayed


# ── Replay dispatch ──────────────────────────────────────────────────────────


async def _dispatch(kind: str, payload: dict[str, Any]) -> bool:
    """Return True on successful replay, False otherwise."""
    try:
        if kind == "fanout":
            return await _replay_fanout(payload)
        if kind == "webhook":
            return await _replay_webhook(payload)
        if kind == "push":
            return await _replay_push(payload)
        # scheduled / notification / sfu_event / unknown: admin-visible only.
        logger.info("dlq_replay_skipped_kind", kind=kind)
        return False
    except Exception as e:
        logger.warning("dlq_dispatch_exception", kind=kind, error=str(e))
        return False


async def _replay_fanout(payload: dict[str, Any]) -> bool:
    """
    Replay a chat message fanout.

    Expected payload shape:
        {
            "event": "chat:new_message" | "v2_chat:new_message",
            "channel_id": str,
            "message": {...},
            "member_ids": [...]   # optional; falls back to channel query
        }
    """
    try:
        from app.socket.server import sio
        from app.services.channel_service import ChannelService
        from app.services.presence_service import presence_service
    except Exception:
        return False

    event = payload.get("event") or "chat:new_message"
    msg = payload.get("message") or {}
    channel_id = payload.get("channel_id") or msg.get("channel_id")
    if not channel_id or not msg:
        return False

    member_ids = payload.get("member_ids")
    if not member_ids:
        try:
            async with async_session_factory() as db:
                channel = await ChannelService.get_channel(db, channel_id)
                member_ids = [m.user_id for m in channel.members]
        except Exception:
            return False

    delivered = 0
    members_seen = 0
    for uid in member_ids or []:
        sids = presence_service.get_sids(uid) or []
        if sids:
            members_seen += 1
        for sid in sids:
            try:
                await sio.emit(event, msg, to=sid)
                delivered += 1
            except Exception:
                pass
    # If nobody was online, the fanout had nothing to do — treat as
    # success rather than retrying the same payload 8 times against an
    # empty audience. Otherwise require at least one delivery.
    if members_seen == 0:
        return True
    return delivered > 0


async def _replay_webhook(payload: dict[str, Any]) -> bool:
    try:
        from app.services.webhook_service import WebhookService
    except Exception:
        return False
    event_name = payload.get("event")
    body = payload.get("payload") or {}
    channel_id = payload.get("channel_id")
    if not event_name:
        return False
    try:
        async with async_session_factory() as db:
            await WebhookService.emit(
                db,
                event=event_name,
                payload=body,
                channel_id=channel_id,
            )
        return True
    except Exception:
        return False


async def _replay_push(payload: dict[str, Any]) -> bool:
    try:
        from app.services.push.dispatcher import push_dispatcher
        from app.services.push.provider import PushPayload
    except Exception:
        return False
    user_ids = payload.get("user_ids") or []
    title = payload.get("title") or ""
    body = payload.get("body")
    data = payload.get("data") or {}
    if not user_ids or not title:
        return False
    try:
        async with async_session_factory() as db:
            await push_dispatcher.dispatch_bulk(
                db, list(user_ids), PushPayload(title=title, body=body, data=data)
            )
        return True
    except Exception:
        return False
