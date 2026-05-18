"""
Scheduled message service — schedule, cancel, list, and dispatch messages
queued for future delivery.

The dispatch worker is started by the application lifespan; it polls for
pending entries whose `send_at` has passed and delivers them via
MessageService.send_message, then broadcasts on the chat sockets.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.scheduled_message import ScheduledMessage
from app.services.channel_service import ChannelService

logger = get_logger(__name__)

# Scheduling guardrails
_MIN_LEAD_TIME_SEC = 5            # Don't accept "schedule for 1 second from now"
_MAX_LEAD_TIME_DAYS = 365         # Don't accept "schedule for 5 years from now"
_MAX_ATTEMPTS = 5
_POLL_INTERVAL_SEC = 15           # How often the worker scans the queue


class ScheduledMessageService:

    @staticmethod
    async def schedule(
        db: AsyncSession,
        sender_id: str,
        channel_id: str,
        content: str,
        send_at: datetime,
        msg_type: str = "text",
        reply_to: str | None = None,
        file_id: str | None = None,
    ) -> ScheduledMessage:
        if not content and not file_id:
            raise ValueError("content or file_id is required")
        if content and len(content) > 10000:
            raise ValueError("content exceeds 10000 chars")

        # Normalize to UTC
        if send_at.tzinfo is None:
            send_at = send_at.replace(tzinfo=timezone.utc)
        else:
            send_at = send_at.astimezone(timezone.utc)

        now = datetime.now(timezone.utc)
        if send_at < now + timedelta(seconds=_MIN_LEAD_TIME_SEC):
            raise ValueError(
                f"send_at must be at least {_MIN_LEAD_TIME_SEC}s in the future"
            )
        if send_at > now + timedelta(days=_MAX_LEAD_TIME_DAYS):
            raise ValueError(
                f"send_at cannot be more than {_MAX_LEAD_TIME_DAYS} days in the future"
            )

        # Verify sender is a member of the channel
        if not await ChannelService.is_member(db, channel_id, sender_id):
            raise ForbiddenError("You are not a member of this channel")

        scheduled = ScheduledMessage(
            sender_id=sender_id,
            channel_id=channel_id,
            content=content or "",
            msg_type=msg_type,
            reply_to=reply_to,
            file_id=file_id,
            send_at=send_at,
            status="pending",
        )
        db.add(scheduled)
        await db.commit()
        await db.refresh(scheduled)
        logger.info(
            "scheduled_message_created",
            id=scheduled.id,
            sender_id=sender_id,
            channel_id=channel_id,
            send_at=send_at.isoformat(),
        )
        return scheduled

    @staticmethod
    async def list_for_user(
        db: AsyncSession,
        sender_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ScheduledMessage]:
        query = select(ScheduledMessage).where(
            ScheduledMessage.sender_id == sender_id
        )
        if status:
            query = query.where(ScheduledMessage.status == status)
        query = query.order_by(ScheduledMessage.send_at.asc()).limit(min(limit, 500))
        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def cancel(
        db: AsyncSession, scheduled_id: str, sender_id: str
    ) -> ScheduledMessage:
        result = await db.execute(
            select(ScheduledMessage).where(ScheduledMessage.id == scheduled_id)
        )
        scheduled = result.scalar_one_or_none()
        if not scheduled:
            raise NotFoundError("ScheduledMessage", scheduled_id)
        if scheduled.sender_id != sender_id:
            raise ForbiddenError("You can only cancel your own scheduled messages")
        if scheduled.status not in ("pending", "failed"):
            raise ValueError(f"Cannot cancel message in status '{scheduled.status}'")
        scheduled.status = "cancelled"
        await db.commit()
        await db.refresh(scheduled)
        logger.info("scheduled_message_cancelled", id=scheduled.id)
        return scheduled

    @staticmethod
    async def update(
        db: AsyncSession,
        scheduled_id: str,
        sender_id: str,
        content: str | None = None,
        send_at: datetime | None = None,
    ) -> ScheduledMessage:
        result = await db.execute(
            select(ScheduledMessage).where(ScheduledMessage.id == scheduled_id)
        )
        scheduled = result.scalar_one_or_none()
        if not scheduled:
            raise NotFoundError("ScheduledMessage", scheduled_id)
        if scheduled.sender_id != sender_id:
            raise ForbiddenError("You can only edit your own scheduled messages")
        if scheduled.status != "pending":
            raise ValueError(f"Cannot edit message in status '{scheduled.status}'")

        if content is not None:
            if len(content) > 10000:
                raise ValueError("content exceeds 10000 chars")
            scheduled.content = content
        if send_at is not None:
            if send_at.tzinfo is None:
                send_at = send_at.replace(tzinfo=timezone.utc)
            else:
                send_at = send_at.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            if send_at < now + timedelta(seconds=_MIN_LEAD_TIME_SEC):
                raise ValueError("send_at must be in the future")
            scheduled.send_at = send_at

        await db.commit()
        await db.refresh(scheduled)
        return scheduled

    # ── Dispatch worker ──────────────────────────────────

    @staticmethod
    async def _claim_due(db: AsyncSession, batch: int = 50) -> list[ScheduledMessage]:
        """
        Fetch pending entries whose send_at has passed.
        SQLite has limited row-level locking, so we use simple status filtering;
        the worker is single-instance so this is safe.
        """
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(ScheduledMessage)
            .where(
                ScheduledMessage.status == "pending",
                ScheduledMessage.send_at <= now,
            )
            .order_by(ScheduledMessage.send_at.asc())
            .limit(batch)
        )
        return list(result.scalars().all())

    @staticmethod
    async def _deliver_one(scheduled: ScheduledMessage) -> None:
        """Deliver one scheduled message and update its state."""
        from app.services.message_service import MessageService
        from app.services.presence_service import presence_service
        from app.socket.server import sio

        async with async_session_factory() as db:
            # Re-load fresh row to avoid stale state
            result = await db.execute(
                select(ScheduledMessage).where(ScheduledMessage.id == scheduled.id)
            )
            row = result.scalar_one_or_none()
            if not row or row.status != "pending":
                return  # Cancelled / already delivered

            row.attempt_count += 1
            row.last_attempt_at = datetime.now(timezone.utc)

            try:
                message = await MessageService.send_message(
                    db,
                    channel_id=row.channel_id,
                    sender_id=row.sender_id,
                    content=row.content,
                    msg_type=row.msg_type,
                    reply_to=row.reply_to,
                    file_id=row.file_id,
                )
                row.status = "sent"
                row.sent_at = datetime.now(timezone.utc)
                row.delivered_message_id = message.id
                row.last_error = None
                await db.commit()

                # ── @mention dispatch (notifications + push) ──
                mentioned_uids: list[str] = []
                try:
                    sender_username = (
                        message.sender.username if message.sender else None
                    )
                    mentioned_uids = await MessageService.dispatch_mentions(
                        db, message, sender_username=sender_username
                    )
                    await db.commit()
                except Exception as e:
                    logger.warning(
                        "scheduled_mention_dispatch_failed",
                        id=row.id,
                        error=str(e),
                    )
                    mentioned_uids = []

                # Real-time notification:new fan-out
                if mentioned_uids:
                    try:
                        m_payload = {
                            "type": "mention",
                            "title": (
                                f"@{sender_username} mentioned you"
                                if sender_username else "You were mentioned"
                            ),
                            "body": (message.content or "")[:280],
                            "reference_id": message.id,
                            "reference_type": "message",
                            "channel_id": row.channel_id,
                            "message_id": message.id,
                            "sender_id": row.sender_id,
                            "sender_username": sender_username,
                            "created_at": (
                                message.created_at.isoformat()
                                if message.created_at else None
                            ),
                        }
                        for uid in mentioned_uids:
                            for n_sid in presence_service.get_sids(uid) or []:
                                try:
                                    await sio.emit("notification:new", m_payload, to=n_sid)
                                except Exception:
                                    pass
                    except Exception as e:
                        logger.warning(
                            "scheduled_mention_socket_failed",
                            id=row.id, error=str(e),
                        )

                # Broadcast on the chat socket so connected clients see it live
                try:
                    channel = await ChannelService.get_channel(db, row.channel_id)
                    msg_payload = {
                        "id": message.id,
                        "channel_id": message.channel_id,
                        "sender": {
                            "id": message.sender.id,
                            "username": message.sender.username,
                            "display_name": message.sender.display_name,
                            "avatar_url": message.sender.avatar_url,
                        } if message.sender else {},
                        "content": message.content,
                        "type": message.type,
                        "reply_to": message.reply_to,
                        "file_id": message.file_id,
                        "scheduled": True,
                        "scheduled_id": row.id,
                        "created_at": message.created_at.isoformat() if message.created_at else None,
                    }
                    for member in channel.members:
                        for m_sid in presence_service.get_sids(member.user_id):
                            await sio.emit("v2_chat:new_message", msg_payload, to=m_sid)
                            await sio.emit("chat:new_message", msg_payload, to=m_sid)
                except Exception as e:
                    logger.warning("scheduled_broadcast_failed", id=row.id, error=str(e))

                logger.info(
                    "scheduled_message_delivered",
                    id=row.id,
                    message_id=message.id,
                )
            except Exception as e:
                row.last_error = str(e)[:500]
                if row.attempt_count >= _MAX_ATTEMPTS:
                    row.status = "failed"
                    logger.error(
                        "scheduled_message_failed",
                        id=row.id,
                        attempts=row.attempt_count,
                        error=str(e),
                    )
                    # DLQ: persist the scheduled-send failure so an
                    # operator can inspect and re-trigger after fixing
                    # the root cause (e.g. broken file reference).
                    try:
                        from app.services.dead_letter_service import record as _dlq_record
                        await _dlq_record(
                            kind="scheduled",
                            reason="scheduled_message_exhausted",
                            error=e,
                            payload={
                                "scheduled_id": row.id,
                                "channel_id": row.channel_id,
                                "sender_id": row.sender_id,
                                "content": (row.content or "")[:2048],
                                "msg_type": row.msg_type,
                                "reply_to": row.reply_to,
                                "file_id": row.file_id,
                            },
                            channel_id=row.channel_id,
                            sender_id=row.sender_id,
                        )
                    except Exception:
                        pass
                else:
                    # Push out by exponential backoff
                    delay = timedelta(seconds=2 ** row.attempt_count * 30)
                    row.send_at = datetime.now(timezone.utc) + delay
                    logger.warning(
                        "scheduled_message_retry",
                        id=row.id,
                        attempt=row.attempt_count,
                        next=row.send_at.isoformat(),
                        error=str(e),
                    )
                await db.commit()

    @staticmethod
    async def run_dispatch_loop(stop_event: asyncio.Event | None = None) -> None:
        """
        Background worker — call from app lifespan.
        Polls every _POLL_INTERVAL_SEC for due entries and dispatches them.
        """
        logger.info("scheduled_message_worker_started", interval=_POLL_INTERVAL_SEC)
        while True:
            try:
                if stop_event and stop_event.is_set():
                    break
                async with async_session_factory() as db:
                    due = await ScheduledMessageService._claim_due(db)
                for entry in due:
                    await ScheduledMessageService._deliver_one(entry)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("scheduled_message_worker_error", error=str(e))
            await asyncio.sleep(_POLL_INTERVAL_SEC)
        logger.info("scheduled_message_worker_stopped")
