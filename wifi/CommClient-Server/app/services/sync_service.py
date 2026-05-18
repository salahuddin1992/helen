"""
SyncService — handles reconnection synchronization and delivery tracking.

Responsibilities:
  1. Track per-recipient message delivery and read status (MessageReceipt)
  2. Provide missed messages after a disconnect (sync window)
  3. Compute accurate unread counts per channel
  4. Bulk mark messages as delivered when a client reconnects
  5. Provide channel-level last message info for channel list

This service operates alongside the existing MessageService — it does NOT
replace any existing functionality.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.models.channel import Channel, ChannelMember
from app.models.message import Message
from app.models.message_status import MessageReceipt

logger = get_logger(__name__)


class SyncService:
    """Stateless service — all state lives in the database."""

    # ── Delivery Receipts ────────────────────────────

    async def create_receipts_for_message(
        self,
        db: AsyncSession,
        message_id: str,
        sender_id: str,
        channel_id: str,
    ) -> list[MessageReceipt]:
        """
        Create delivery receipt rows for all channel members except the sender.
        Called when a new message is persisted.

        Uses SQLAlchemy Core executemany so a 10 000-member channel becomes
        one INSERT statement with a values-list instead of 10 000 ORM ``add``
        calls. Callers do not consume the returned objects, so we return an
        empty list to keep the old signature compatible.
        """
        from sqlalchemy import insert as _sql_insert
        from app.db.base import generate_uuid

        stmt = select(ChannelMember.user_id).where(
            and_(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id != sender_id,
            )
        )
        member_ids = [row[0] for row in (await db.execute(stmt)).all()]
        if not member_ids:
            return []

        now = datetime.now(timezone.utc)
        rows = [
            {
                "id": generate_uuid(),
                "message_id": message_id,
                "recipient_id": uid,
                "delivered_at": None,
                "read_at": None,
                "created_at": now,
                "updated_at": now,
            }
            for uid in member_ids
        ]
        # Single INSERT with multi-values — ~50-100× faster than ORM per-row.
        await db.execute(_sql_insert(MessageReceipt), rows)
        return []

    async def mark_delivered(
        self,
        db: AsyncSession,
        message_id: str,
        recipient_id: str,
    ) -> MessageReceipt | None:
        """Mark a single message as delivered to a recipient."""
        stmt = select(MessageReceipt).where(
            and_(
                MessageReceipt.message_id == message_id,
                MessageReceipt.recipient_id == recipient_id,
            )
        )
        result = await db.execute(stmt)
        receipt = result.scalar_one_or_none()

        if receipt:
            receipt.mark_delivered()
            await db.flush()

        return receipt

    async def bulk_mark_delivered(
        self,
        db: AsyncSession,
        recipient_id: str,
        message_ids: list[str] | None = None,
        channel_id: str | None = None,
    ) -> int:
        """
        Bulk mark messages as delivered for a recipient.
        Either by explicit message_ids or all undelivered in a channel.
        Returns count of updated receipts.
        """
        now = datetime.now(timezone.utc)

        conditions = [
            MessageReceipt.recipient_id == recipient_id,
            MessageReceipt.delivered_at.is_(None),
        ]

        if message_ids:
            conditions.append(MessageReceipt.message_id.in_(message_ids))
        elif channel_id:
            # Get message IDs in this channel
            msg_subq = select(Message.id).where(Message.channel_id == channel_id)
            conditions.append(MessageReceipt.message_id.in_(msg_subq))
        else:
            # Mark ALL undelivered messages for this user
            pass

        stmt = (
            update(MessageReceipt)
            .where(and_(*conditions))
            .values(delivered_at=now)
        )
        result = await db.execute(stmt)
        await db.flush()

        count = result.rowcount
        if count:
            logger.info(
                "bulk_delivered",
                recipient_id=recipient_id,
                count=count,
                channel_id=channel_id,
            )
        return count

    async def mark_read(
        self,
        db: AsyncSession,
        channel_id: str,
        recipient_id: str,
        up_to_message_id: str | None = None,
    ) -> int:
        """
        Mark messages as read in a channel for a recipient.
        If up_to_message_id is provided, marks all messages up to (and including) that one.
        Otherwise marks all messages in the channel.
        Returns count of updated receipts.
        """
        now = datetime.now(timezone.utc)

        # Get messages in channel
        msg_conditions = [Message.channel_id == channel_id]

        if up_to_message_id:
            # Get the timestamp of the target message
            target_stmt = select(Message.created_at).where(Message.id == up_to_message_id)
            target_result = await db.execute(target_stmt)
            target_ts = target_result.scalar_one_or_none()
            if target_ts:
                msg_conditions.append(Message.created_at <= target_ts)

        msg_subq = select(Message.id).where(and_(*msg_conditions))

        stmt = (
            update(MessageReceipt)
            .where(
                and_(
                    MessageReceipt.recipient_id == recipient_id,
                    MessageReceipt.message_id.in_(msg_subq),
                    MessageReceipt.read_at.is_(None),
                )
            )
            .values(
                read_at=now,
                delivered_at=func.coalesce(MessageReceipt.delivered_at, now),
            )
        )
        result = await db.execute(stmt)
        await db.flush()

        # Also update ChannelMember.last_read_at
        member_stmt = (
            update(ChannelMember)
            .where(
                and_(
                    ChannelMember.channel_id == channel_id,
                    ChannelMember.user_id == recipient_id,
                )
            )
            .values(last_read_at=now)
        )
        await db.execute(member_stmt)
        await db.flush()

        count = result.rowcount
        if count:
            logger.info(
                "bulk_read",
                recipient_id=recipient_id,
                channel_id=channel_id,
                count=count,
            )
        return count

    async def get_message_receipts(
        self,
        db: AsyncSession,
        message_id: str,
    ) -> list[dict]:
        """Get all receipts for a specific message."""
        stmt = select(MessageReceipt).where(
            MessageReceipt.message_id == message_id
        )
        result = await db.execute(stmt)
        receipts = result.scalars().all()
        return [r.to_dict() for r in receipts]

    # ── Reconnection Sync ────────────────────────────

    async def get_missed_messages(
        self,
        db: AsyncSession,
        user_id: str,
        since: datetime,
        limit: int = 500,
    ) -> dict[str, list[dict]]:
        """
        Get all messages across all user's channels since a given timestamp.
        Returns dict: channel_id → [message_dicts] (newest last).

        Used when client reconnects after a disconnect.
        """
        # Get user's channel IDs
        ch_stmt = select(ChannelMember.channel_id).where(
            ChannelMember.user_id == user_id
        )
        ch_result = await db.execute(ch_stmt)
        channel_ids = [row[0] for row in ch_result.all()]

        if not channel_ids:
            return {}

        # Fetch messages since timestamp
        msg_stmt = (
            select(Message)
            .where(
                and_(
                    Message.channel_id.in_(channel_ids),
                    Message.created_at > since,
                    Message.deleted_at.is_(None),
                )
            )
            .options(selectinload(Message.sender))
            .order_by(Message.created_at.asc())
            .limit(limit)
        )
        result = await db.execute(msg_stmt)
        messages = result.scalars().all()

        # Group by channel
        grouped: dict[str, list[dict]] = {}
        for msg in messages:
            ch_id = msg.channel_id
            if ch_id not in grouped:
                grouped[ch_id] = []
            grouped[ch_id].append({
                "id": msg.id,
                "channel_id": msg.channel_id,
                "sender": {
                    "id": msg.sender.id,
                    "username": msg.sender.username,
                    "display_name": msg.sender.display_name,
                    "avatar_url": msg.sender.avatar_url,
                } if msg.sender else None,
                "content": msg.content,
                "type": msg.type,
                "reply_to": msg.reply_to,
                "file_id": msg.file_id,
                "status": msg.status,
                "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            })

        logger.info(
            "sync_missed_messages",
            user_id=user_id,
            since=since.isoformat(),
            channel_count=len(grouped),
            message_count=sum(len(v) for v in grouped.values()),
        )
        return grouped

    # ── Unread Counts ────────────────────────────────

    async def get_unread_counts(
        self,
        db: AsyncSession,
        user_id: str,
    ) -> dict[str, dict]:
        """
        Get unread message counts per channel for a user.
        Returns: { channel_id: { unread: int, last_message: {...} } }
        """
        # Get user's channels with last_read_at
        member_stmt = select(
            ChannelMember.channel_id,
            ChannelMember.last_read_at,
        ).where(ChannelMember.user_id == user_id)
        member_result = await db.execute(member_stmt)
        memberships = {row[0]: row[1] for row in member_result.all()}

        if not memberships:
            return {}

        result: dict[str, dict] = {}

        for channel_id, last_read_at in memberships.items():
            # Count unread messages
            count_conditions = [
                Message.channel_id == channel_id,
                Message.sender_id != user_id,
                Message.deleted_at.is_(None),
            ]
            if last_read_at:
                count_conditions.append(Message.created_at > last_read_at)

            count_stmt = select(func.count(Message.id)).where(and_(*count_conditions))
            count_result = await db.execute(count_stmt)
            unread = count_result.scalar() or 0

            # Get last message in channel
            last_msg_stmt = (
                select(Message)
                .where(
                    and_(
                        Message.channel_id == channel_id,
                        Message.deleted_at.is_(None),
                    )
                )
                .options(selectinload(Message.sender))
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            last_msg_result = await db.execute(last_msg_stmt)
            last_msg = last_msg_result.scalar_one_or_none()

            last_message = None
            if last_msg:
                last_message = {
                    "id": last_msg.id,
                    "sender_id": last_msg.sender_id,
                    "sender_name": (
                        last_msg.sender.display_name or last_msg.sender.username
                    ) if last_msg.sender else "Unknown",
                    "content": last_msg.content[:100],  # Preview truncated
                    "type": last_msg.type,
                    "created_at": last_msg.created_at.isoformat() if last_msg.created_at else None,
                }

            result[channel_id] = {
                "unread": unread,
                "last_message": last_message,
            }

        return result

    # ── Channel Last Messages (batch) ────────────────

    async def get_channel_summaries(
        self,
        db: AsyncSession,
        user_id: str,
    ) -> list[dict]:
        """
        Get channel summaries for channel list rendering.
        Returns: [{ channel_id, unread, last_message, typing_users: [] }]
        """
        unread_data = await self.get_unread_counts(db, user_id)

        summaries = []
        for channel_id, data in unread_data.items():
            summaries.append({
                "channel_id": channel_id,
                "unread": data["unread"],
                "last_message": data["last_message"],
            })

        # Sort by last message time (newest first)
        summaries.sort(
            key=lambda s: s["last_message"]["created_at"] if s.get("last_message") else "",
            reverse=True,
        )
        return summaries

    # ── Enhanced Sync & Delivery ─────────────────────

    async def sync_and_confirm_delivery(
        self,
        db: AsyncSession,
        user_id: str,
        since_timestamp: datetime,
        channels: list[str] | None = None,
    ) -> dict:
        """
        Fetch missed messages AND mark them all as delivered in one operation.
        Atomically fetches missed messages and confirms delivery to the recipient.

        Returns:
        {
            "missed_messages": { channel_id → [message_dicts] },
            "delivery_confirmed_count": int,
            "sync_timestamp": ISO string
        }
        """
        # Get user's channel IDs
        ch_stmt = select(ChannelMember.channel_id).where(
            ChannelMember.user_id == user_id
        )
        ch_result = await db.execute(ch_stmt)
        user_channel_ids = [row[0] for row in ch_result.all()]

        if not user_channel_ids:
            return {
                "missed_messages": {},
                "delivery_confirmed_count": 0,
                "sync_timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # Filter to requested channels if provided
        if channels:
            user_channel_ids = [c for c in user_channel_ids if c in channels]

        # Fetch missed messages
        msg_stmt = (
            select(Message)
            .where(
                and_(
                    Message.channel_id.in_(user_channel_ids),
                    Message.created_at > since_timestamp,
                    Message.deleted_at.is_(None),
                )
            )
            .options(selectinload(Message.sender))
            .order_by(Message.created_at.asc())
        )
        result = await db.execute(msg_stmt)
        messages = result.scalars().all()

        # Collect message IDs for bulk delivery confirmation
        message_ids = [m.id for m in messages]

        # Mark all as delivered for this user
        if message_ids:
            await self.bulk_mark_delivered(db, user_id, message_ids=message_ids)

        # Group by channel
        grouped: dict[str, list[dict]] = {}
        for msg in messages:
            ch_id = msg.channel_id
            if ch_id not in grouped:
                grouped[ch_id] = []
            grouped[ch_id].append({
                "id": msg.id,
                "channel_id": msg.channel_id,
                "sender": {
                    "id": msg.sender.id,
                    "username": msg.sender.username,
                    "display_name": msg.sender.display_name,
                    "avatar_url": msg.sender.avatar_url,
                } if msg.sender else None,
                "content": msg.content,
                "type": msg.type,
                "reply_to": msg.reply_to,
                "file_id": msg.file_id,
                "status": msg.status,
                "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            })

        logger.info(
            "sync_and_confirm_delivery",
            user_id=user_id,
            since=since_timestamp.isoformat(),
            channel_count=len(grouped),
            message_count=len(message_ids),
        )

        return {
            "missed_messages": grouped,
            "delivery_confirmed_count": len(message_ids),
            "sync_timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def get_channel_read_states(
        self,
        db: AsyncSession,
        channel_id: str,
    ) -> list[dict]:
        """
        Return per-member read state snapshot for a channel.
        Shows who has read up to what point.

        Returns:
        [
            {
                "user_id": str,
                "username": str,
                "display_name": str,
                "last_read_at": ISO string or None,
                "unread_count": int
            },
            ...
        ]
        """
        # Get all channel members with their last read timestamps
        member_stmt = (
            select(ChannelMember)
            .where(ChannelMember.channel_id == channel_id)
            .options(selectinload(ChannelMember.user))
        )
        result = await db.execute(member_stmt)
        members = result.scalars().all()

        states = []
        for member in members:
            # Count unread messages for this member
            unread_conditions = [
                Message.channel_id == channel_id,
                Message.sender_id != member.user_id,
                Message.deleted_at.is_(None),
            ]
            if member.last_read_at:
                unread_conditions.append(Message.created_at > member.last_read_at)

            count_stmt = select(func.count(Message.id)).where(and_(*unread_conditions))
            count_result = await db.execute(count_stmt)
            unread = count_result.scalar() or 0

            states.append({
                "user_id": member.user_id,
                "username": member.user.username if member.user else "Unknown",
                "display_name": member.user.display_name if member.user else None,
                "last_read_at": member.last_read_at.isoformat() if member.last_read_at else None,
                "unread_count": unread,
            })

        logger.info(
            "get_channel_read_states",
            channel_id=channel_id,
            member_count=len(states),
        )
        return states

    async def get_receipt_summary(
        self,
        db: AsyncSession,
        message_id: str,
    ) -> dict:
        """
        Return delivery/read status summary for a message.

        Returns:
        {
            "message_id": str,
            "delivered_count": int,
            "read_count": int,
            "total_recipients": int,
            "recipients": [
                {
                    "user_id": str,
                    "username": str,
                    "status": "undelivered" | "delivered" | "read",
                    "delivered_at": ISO string or None,
                    "read_at": ISO string or None
                },
                ...
            ]
        }
        """
        # Fetch all receipts for this message
        stmt = (
            select(MessageReceipt)
            .where(MessageReceipt.message_id == message_id)
            .options(selectinload(MessageReceipt.recipient))
        )
        result = await db.execute(stmt)
        receipts = result.scalars().all()

        delivered_count = 0
        read_count = 0
        recipients = []

        for receipt in receipts:
            if receipt.read_at:
                status = "read"
                read_count += 1
            elif receipt.delivered_at:
                status = "delivered"
                delivered_count += 1
            else:
                status = "undelivered"

            recipients.append({
                "user_id": receipt.recipient_id,
                "username": receipt.recipient.username if receipt.recipient else "Unknown",
                "status": status,
                "delivered_at": receipt.delivered_at.isoformat() if receipt.delivered_at else None,
                "read_at": receipt.read_at.isoformat() if receipt.read_at else None,
            })

        logger.info(
            "get_receipt_summary",
            message_id=message_id,
            delivered=delivered_count,
            read=read_count,
            total=len(receipts),
        )

        return {
            "message_id": message_id,
            "delivered_count": delivered_count,
            "read_count": read_count,
            "total_recipients": len(receipts),
            "recipients": recipients,
        }


# Singleton
sync_service = SyncService()
