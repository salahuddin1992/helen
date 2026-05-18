"""
Messaging service — send, retrieve, search, react.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.models.channel import ChannelMember
from app.models.message import Message, Reaction
from app.models.user import User
from app.services.channel_service import ChannelService

logger = get_logger(__name__)

# Match @username — alphanumeric + underscore, 2..64 chars (matches User.username)
# Avoid matching email addresses by requiring start-of-string or non-word char before @
_MENTION_RE = re.compile(r"(?:^|[^\w])@([A-Za-z0-9_]{2,64})")


class MessageService:

    @staticmethod
    def extract_mentions(content: str) -> list[str]:
        """
        Extract @username mentions from message content.
        Returns list of unique lowercase usernames (without @ prefix), preserving order.
        Matches alphanumeric + underscore, 2..64 chars; rejects @ inside emails or words.
        """
        if not content:
            return []
        seen: set[str] = set()
        ordered: list[str] = []
        for match in _MENTION_RE.finditer(content):
            uname = match.group(1).lower()
            if uname not in seen:
                seen.add(uname)
                ordered.append(uname)
        return ordered

    @staticmethod
    async def resolve_mentioned_members(
        db: AsyncSession,
        channel_id: str,
        usernames: list[str],
        exclude_user_id: str | None = None,
    ) -> list[User]:
        """
        Resolve a list of usernames to User rows that are members of the given channel.
        - Case-insensitive username match
        - Filters out exclude_user_id (typically the sender)
        - Filters out @everyone / @channel — caller handles those separately
        """
        if not usernames:
            return []
        # Strip special tokens
        real = [u for u in usernames if u not in ("everyone", "channel", "here", "all")]
        if not real:
            return []

        # Lookup members joined with users in one query
        result = await db.execute(
            select(User)
            .join(ChannelMember, ChannelMember.user_id == User.id)
            .where(
                ChannelMember.channel_id == channel_id,
                func.lower(User.username).in_(real),
            )
        )
        users = list(result.scalars().all())
        if exclude_user_id:
            users = [u for u in users if u.id != exclude_user_id]
        return users

    @staticmethod
    async def send_message(
        db: AsyncSession,
        channel_id: str,
        sender_id: str,
        content: str,
        msg_type: str = "text",
        reply_to: str | None = None,
        file_id: str | None = None,
        client_message_id: str | None = None,
    ) -> Message:
        """Send a message to a channel. Validates membership, block, ban,
        and idempotency.

        ``client_message_id`` is an opaque token from the caller. If a
        prior message already exists with the same (sender_id,
        client_message_id) we return that row instead of creating a
        duplicate — protects against client retries / network blips.
        """
        if content and len(content) > 10000:
            raise ValueError("Message content exceeds maximum length of 10000 characters")

        # Slow-mode enforcement. Looks up the per-channel cap from
        # ``app.services.channel_slow_mode`` (JSON-backed, no DB
        # column required). Admins bypass — they're typically the
        # ones moderating the slow chat.
        try:
            from app.services.channel_slow_mode import (
                check_send_allowed, ChannelSlowModeError,
            )
            # Determine admin status via channel role; fail-soft if
            # the lookup itself errors (we don't want slow-mode to
            # crash sends).
            is_admin = False
            try:
                role_row = await db.execute(
                    select(ChannelMember.role).where(
                        ChannelMember.channel_id == channel_id,
                        ChannelMember.user_id == sender_id,
                    ),
                )
                role = role_row.scalar_one_or_none()
                is_admin = role == "admin"
            except Exception:
                pass
            check_send_allowed(channel_id, sender_id, is_admin=is_admin)
        except ChannelSlowModeError as smex:
            # Re-raise as ValueError so the existing route layer
            # surfaces it as a 400 with a parseable detail. Format:
            # "slow_mode:<seconds>" so the client can render a
            # countdown without parsing a free-form string.
            raise ValueError(
                f"slow_mode:{smex.wait_seconds:.1f}",
            )

        # ── Idempotency on (sender, client_message_id) ──
        # Cheap pre-check; the DB UniqueConstraint is the actual
        # boundary on race conditions.
        if client_message_id:
            dup = await db.execute(
                select(Message).where(
                    Message.sender_id == sender_id,
                    Message.client_message_id == client_message_id,
                ).limit(1)
            )
            existing = dup.scalar_one_or_none()
            if existing is not None:
                logger.info(
                    "message_idempotent_replay",
                    message_id=existing.id,
                    client_message_id=client_message_id,
                    sender_id=sender_id,
                )
                # Still load relationships so caller's downstream code
                # (sender, reactions) gets the same shape.
                result = await db.execute(
                    select(Message)
                    .where(Message.id == existing.id)
                    .options(
                        selectinload(Message.sender),
                        selectinload(Message.reactions),
                    )
                )
                return result.scalar_one()

        if not await ChannelService.is_member(db, channel_id, sender_id):
            raise ForbiddenError("You are not a member of this channel")

        from app.models.channel import Channel as _Channel, ChannelMember as _ChannelMember
        ch_row = await db.execute(
            select(_Channel.type).where(_Channel.id == channel_id)
        )
        ch_type = ch_row.scalar_one_or_none()

        # ── Group ban check (audit fix 1.4) ──
        # A member with banned_until > now (or NULL banned_until but
        # banned_at set = permanent) cannot send. Applies to ALL channel
        # types, not just groups, but the column lives on ChannelMember
        # which is irrelevant for DMs (handled below).
        try:
            mine = (await db.execute(
                select(_ChannelMember).where(
                    _ChannelMember.channel_id == channel_id,
                    _ChannelMember.user_id == sender_id,
                )
            )).scalar_one_or_none()
            if mine is not None:
                from datetime import datetime as _dt, timezone as _tz
                banned_at = getattr(mine, "banned_at", None)
                banned_until = getattr(mine, "banned_until", None)
                if banned_at is not None:
                    if banned_until is None or banned_until > _dt.now(_tz.utc):
                        raise ForbiddenError(
                            "You have been banned from this channel.",
                        )
        except ForbiddenError:
            raise
        except Exception as _e:
            logger.debug("group_ban_check_failed", error=str(_e))

        # ── Block enforcement on DM channels ──
        # Look up channel type + members; for DMs reject if either side blocked the other.
        if ch_type == "dm":
            other_row = await db.execute(
                select(_ChannelMember.user_id).where(
                    _ChannelMember.channel_id == channel_id,
                    _ChannelMember.user_id != sender_id,
                )
            )
            other_id = other_row.scalar_one_or_none()
            if other_id:
                from app.services.user_service import UserService as _US
                blocked, blocker = await _US.is_blocked_either_way(db, sender_id, other_id)
                if blocked:
                    if blocker == sender_id:
                        raise ForbiddenError(
                            "You have blocked this user. Unblock them to send messages."
                        )
                    raise ForbiddenError(
                        "You cannot send messages to this user."
                    )

        message = Message(
            channel_id=channel_id,
            sender_id=sender_id,
            content=content,
            type=msg_type,
            reply_to=reply_to,
            file_id=file_id,
            client_message_id=client_message_id,
        )
        # ── Atomic message + receipts (audit fix 2.8) ──
        # Wrap both inserts in one savepoint so a crash between them
        # never leaves a message without delivery rows. We cannot
        # simply call db.begin() because this method is invoked under
        # an existing session that the caller may already have an
        # outer transaction on. Use begin_nested (savepoint) which
        # works in both cases.
        from app.services.sync_service import SyncService
        sync_svc = SyncService()
        async with db.begin_nested():
            db.add(message)
            await db.flush()  # assigns message.id
            try:
                await sync_svc.create_receipts_for_message(
                    db, message.id, sender_id, channel_id,
                )
            except Exception:
                # Receipts are advisory but missing them produces silent
                # "delivered/read" UX bugs — surface and roll back.
                raise
        await db.commit()

        # Reload with relationships
        result = await db.execute(
            select(Message)
            .where(Message.id == message.id)
            .options(
                selectinload(Message.sender),
                selectinload(Message.reactions),
            )
        )
        message = result.scalar_one()
        logger.info("message_sent", message_id=message.id, channel_id=channel_id)

        # Best-effort: enqueue an outbound webhook event
        try:
            from app.services.webhook_service import WebhookService
            await WebhookService.emit(
                db,
                event="message.created",
                payload={
                    "message_id": message.id,
                    "channel_id": channel_id,
                    "sender_id": sender_id,
                    "sender_username": getattr(message.sender, "username", None),
                    "type": msg_type,
                    "content": content,
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                },
                channel_id=channel_id,
            )
        except Exception as e:  # never let webhook issues block sends
            logger.warning("webhook_emit_failed", event_name="message.created", error=str(e))

        return message

    @staticmethod
    async def dispatch_mentions(
        db: AsyncSession,
        message: Message,
        sender_username: str | None = None,
    ) -> list[str]:
        """
        Parse @mentions from a message, persist Notification rows for mentioned users,
        and return the list of mentioned user IDs (excluding sender).

        Uses NotificationService for storage. Caller is responsible for emitting
        socket events to mentioned users (so we don't import socket layer here).
        """
        if not message.content:
            return []

        usernames = MessageService.extract_mentions(message.content)
        if not usernames:
            return []

        # Handle @everyone / @channel — expand to all channel members
        broadcast_all = any(
            tok in usernames for tok in ("everyone", "channel", "here", "all")
        )

        from app.services.notification_service import NotificationService

        mentioned_user_ids: list[str] = []

        if broadcast_all:
            # Pull every member of the channel except sender
            mem_result = await db.execute(
                select(ChannelMember.user_id).where(
                    ChannelMember.channel_id == message.channel_id,
                    ChannelMember.user_id != message.sender_id,
                )
            )
            mentioned_user_ids = [row[0] for row in mem_result.all()]
        else:
            users = await MessageService.resolve_mentioned_members(
                db, message.channel_id, usernames, exclude_user_id=message.sender_id
            )
            mentioned_user_ids = [u.id for u in users]

        if not mentioned_user_ids:
            return []

        title = f"@{sender_username} mentioned you" if sender_username else "You were mentioned"
        # Truncate body to a reasonable preview length
        preview = (message.content or "")[:280]

        try:
            await NotificationService.create_bulk(
                db=db,
                user_ids=mentioned_user_ids,
                type="mention",
                title=title,
                body=preview,
                reference_id=message.id,
                reference_type="message",
            )
        except Exception as e:
            logger.warning(
                "mention_notification_create_failed",
                message_id=message.id,
                error=str(e),
            )

        logger.info(
            "mentions_dispatched",
            message_id=message.id,
            channel_id=message.channel_id,
            count=len(mentioned_user_ids),
            broadcast=broadcast_all,
        )
        return mentioned_user_ids

    @staticmethod
    async def get_messages(
        db: AsyncSession,
        channel_id: str,
        user_id: str,
        before: datetime | None = None,
        limit: int = 50,
    ) -> tuple[list[Message], bool, int]:
        """Get paginated messages for a channel. Returns (messages, has_more, total)."""
        if not await ChannelService.is_member(db, channel_id, user_id):
            raise ForbiddenError("You are not a member of this channel")

        query = (
            select(Message)
            .where(
                Message.channel_id == channel_id,
                Message.deleted_at.is_(None),
            )
            .options(
                selectinload(Message.sender),
                selectinload(Message.reactions),
            )
        )

        if before:
            query = query.where(Message.created_at < before)

        # Total count
        count_query = select(func.count()).select_from(Message).where(
            Message.channel_id == channel_id,
            Message.deleted_at.is_(None),
        )
        total = (await db.execute(count_query)).scalar() or 0

        # Fetch limit + 1 to detect has_more
        result = await db.execute(
            query.order_by(Message.created_at.desc()).limit(limit + 1)
        )
        messages = list(result.scalars().all())

        has_more = len(messages) > limit
        if has_more:
            messages = messages[:limit]

        return messages, has_more, total

    @staticmethod
    async def search_messages(
        db: AsyncSession,
        user_id: str,
        query_text: str | None = None,
        channel_id: str | None = None,
        sender_id: str | None = None,
        sender_username: str | None = None,
        msg_type: str | None = None,
        has_file: bool | None = None,
        has_reactions: bool | None = None,
        is_pinned: bool | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Message], int]:
        """
        Advanced message search with multiple optional filters.
        - query_text: substring of message content (case-insensitive)
        - channel_id: limit to a single channel
        - sender_id / sender_username: limit to a sender
        - msg_type: text/file/image/reply/system
        - has_file: messages with/without an attached file_id
        - has_reactions: messages that have any reactions
        - is_pinned: only pinned messages
        - date_from / date_to: created_at range
        """
        # Get user's channel IDs
        ch_result = await db.execute(
            select(ChannelMember.channel_id).where(ChannelMember.user_id == user_id)
        )
        user_channel_ids = [r[0] for r in ch_result.all()]

        if not user_channel_ids:
            return [], 0

        # Channel scoping
        if channel_id:
            if not await ChannelService.is_member(db, channel_id, user_id):
                raise ForbiddenError("You are not a member of this channel")
            channel_filter = Message.channel_id == channel_id
        else:
            channel_filter = Message.channel_id.in_(user_channel_ids)

        where_clauses: list = [
            channel_filter,
            Message.deleted_at.is_(None),
        ]

        # Text query
        if query_text:
            escaped = (
                query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            where_clauses.append(Message.content.ilike(f"%{escaped}%", escape="\\"))

        # Sender filter (by id OR by username)
        if sender_id:
            where_clauses.append(Message.sender_id == sender_id)
        elif sender_username:
            uname = sender_username.lstrip("@").lower()
            sub = select(User.id).where(func.lower(User.username) == uname)
            where_clauses.append(Message.sender_id.in_(sub))

        if msg_type:
            where_clauses.append(Message.type == msg_type)

        if has_file is True:
            where_clauses.append(Message.file_id.isnot(None))
        elif has_file is False:
            where_clauses.append(Message.file_id.is_(None))

        if is_pinned is True:
            where_clauses.append(Message.pinned_at.isnot(None))
        elif is_pinned is False:
            where_clauses.append(Message.pinned_at.is_(None))

        if date_from:
            where_clauses.append(Message.created_at >= date_from)
        if date_to:
            where_clauses.append(Message.created_at <= date_to)

        # Reactions filter requires a JOIN — apply via correlated EXISTS
        if has_reactions is True:
            sub = select(Reaction.message_id).where(Reaction.message_id == Message.id)
            where_clauses.append(sub.exists())
        elif has_reactions is False:
            sub = select(Reaction.message_id).where(Reaction.message_id == Message.id)
            where_clauses.append(~sub.exists())

        count_q = select(func.count()).select_from(Message).where(*where_clauses)
        total = (await db.execute(count_q)).scalar() or 0

        result = await db.execute(
            select(Message)
            .where(*where_clauses)
            .options(selectinload(Message.sender), selectinload(Message.reactions))
            .order_by(Message.created_at.desc())
            .offset(max(0, offset))
            .limit(min(limit, 200))
        )
        return list(result.scalars().all()), total

    @staticmethod
    async def edit_message(
        db: AsyncSession,
        message_id: str,
        user_id: str,
        new_content: str,
    ) -> Message:
        if not new_content or not new_content.strip():
            raise ValueError("Message content cannot be empty")

        result = await db.execute(
            select(Message).where(Message.id == message_id)
            .options(selectinload(Message.sender), selectinload(Message.reactions))
        )
        message = result.scalar_one_or_none()
        if not message:
            raise NotFoundError("Message", message_id)
        if message.sender_id != user_id:
            raise ForbiddenError("You can only edit your own messages")

        # No-op edit: don't record history if the content didn't change
        if message.content == new_content:
            return message

        # Snapshot previous content into edit history (append-only audit trail)
        from app.models.message_edit_history import MessageEditHistory
        db.add(
            MessageEditHistory(
                message_id=message.id,
                editor_id=user_id,
                previous_content=message.content,
                edited_at=datetime.now(timezone.utc),
            )
        )

        prev_content = message.content
        message.content = new_content
        message.edited_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(message)

        # ── Mention diff: dispatch only newly-added @mentions ──
        # Prevents re-notifying users who were already mentioned in the
        # original message (common when users fix typos).
        try:
            prev_set = set(MessageService.extract_mentions(prev_content or ""))
            curr_set = set(MessageService.extract_mentions(new_content or ""))
            added = curr_set - prev_set
            if added:
                # Build a synthetic message-like object with only the new
                # usernames embedded so dispatch_mentions picks just those.
                # Easier: call resolve_mentioned_members directly + create
                # notifications via NotificationService.
                users = await MessageService.resolve_mentioned_members(
                    db,
                    message.channel_id,
                    list(added),
                    exclude_user_id=message.sender_id,
                )
                mentioned_ids = [u.id for u in users]
                if mentioned_ids:
                    from app.services.notification_service import NotificationService
                    sender_username = (
                        message.sender.username if message.sender else None
                    )
                    title = (
                        f"@{sender_username} mentioned you"
                        if sender_username else "You were mentioned"
                    )
                    preview = (new_content or "")[:280]
                    await NotificationService.create_bulk(
                        db=db,
                        user_ids=mentioned_ids,
                        type="mention",
                        title=title,
                        body=preview,
                        reference_id=message.id,
                        reference_type="message",
                    )
                    logger.info(
                        "mention_edit_dispatched",
                        message_id=message.id,
                        added_count=len(mentioned_ids),
                    )
        except Exception as e:
            logger.warning(
                "mention_edit_dispatch_failed",
                message_id=message.id,
                error=str(e),
            )

        return message

    @staticmethod
    async def get_edit_history(
        db: AsyncSession,
        message_id: str,
        user_id: str,
    ) -> list:
        """
        Return the edit history for a message, oldest first.
        Caller must be a member of the message's channel.
        """
        from app.models.message_edit_history import MessageEditHistory

        msg = await db.get(Message, message_id)
        if msg is None or msg.deleted_at is not None:
            raise NotFoundError("Message", message_id)
        if not await ChannelService.is_member(db, msg.channel_id, user_id):
            raise ForbiddenError("Not a member of this channel")

        result = await db.execute(
            select(MessageEditHistory)
            .where(MessageEditHistory.message_id == message_id)
            .order_by(MessageEditHistory.edited_at.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def delete_message(
        db: AsyncSession,
        message_id: str,
        user_id: str,
    ) -> Message:
        result = await db.execute(select(Message).where(Message.id == message_id))
        message = result.scalar_one_or_none()
        if not message:
            raise NotFoundError("Message", message_id)
        # Senders can always delete their own messages. Channel admins
        # and moderators can also delete messages from others
        # (audit fix — previously the only path was sender-only, leaving
        # channel mods unable to remove abusive content without raw DB
        # access).
        if message.sender_id != user_id:
            is_mod = await ChannelService.is_admin_or_moderator(
                db, message.channel_id, user_id,
            )
            if not is_mod:
                raise ForbiddenError(
                    "You can only delete your own messages "
                    "(or you must be a channel admin/moderator)"
                )

        message.deleted_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info("message_deleted", message_id=message_id)
        return message

    @staticmethod
    async def toggle_reaction(
        db: AsyncSession,
        message_id: str,
        user_id: str,
        emoji: str,
    ) -> list[Reaction]:
        """Toggle a reaction on a message. Verifies channel membership."""
        # Verify message exists
        msg_result = await db.execute(select(Message).where(Message.id == message_id))
        msg = msg_result.scalar_one_or_none()
        if not msg:
            raise NotFoundError("Message", message_id)

        # SECURITY: Verify user is member of the message's channel
        if not await ChannelService.is_member(db, msg.channel_id, user_id):
            raise ForbiddenError("Not a member of this channel")

        existing = await db.execute(
            select(Reaction).where(
                Reaction.message_id == message_id,
                Reaction.user_id == user_id,
                Reaction.emoji == emoji,
            )
        )
        reaction = existing.scalar_one_or_none()

        if reaction:
            await db.delete(reaction)
        else:
            db.add(Reaction(
                message_id=message_id,
                user_id=user_id,
                emoji=emoji,
            ))

        await db.commit()

        # Return all reactions for this message
        result = await db.execute(
            select(Reaction).where(Reaction.message_id == message_id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def mark_read(
        db: AsyncSession,
        channel_id: str,
        user_id: str,
        message_id: str,
    ) -> None:
        """Mark messages as read up to a specific message."""
        result = await db.execute(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel_id,
                ChannelMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member:
            member.last_read_at = datetime.now(timezone.utc)
            await db.commit()

    @staticmethod
    def aggregate_reactions(reactions: list[Reaction]) -> list[dict]:
        """Aggregate reactions into {emoji, count, user_ids} format."""
        grouped: dict[str, list[str]] = defaultdict(list)
        for r in reactions:
            grouped[r.emoji].append(r.user_id)
        return [
            {"emoji": emoji, "count": len(uids), "user_ids": uids}
            for emoji, uids in grouped.items()
        ]

    @staticmethod
    async def get_thread(
        db: AsyncSession,
        message_id: str,
        user_id: str,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[Message]:
        """
        Fetch all replies to a given message (thread).
        Returns messages that reply_to the given message_id.
        """
        # Fetch the parent message to get channel_id and verify membership
        parent_result = await db.execute(select(Message).where(Message.id == message_id))
        parent_message = parent_result.scalar_one_or_none()
        if not parent_message:
            raise NotFoundError("Message", message_id)

        # Verify user is a member of the channel
        if not await ChannelService.is_member(db, parent_message.channel_id, user_id):
            raise ForbiddenError("You are not a member of this channel")

        query = (
            select(Message)
            .where(
                Message.reply_to == message_id,
                Message.deleted_at.is_(None),
            )
            .options(
                selectinload(Message.sender),
                selectinload(Message.reactions),
            )
        )

        if before:
            query = query.where(Message.created_at < before)

        result = await db.execute(
            query.order_by(Message.created_at.asc()).limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def forward_message(
        db: AsyncSession,
        message_id: str,
        to_channel_id: str,
        user_id: str,
    ) -> Message:
        """
        Forward a message to another channel.
        Creates a new message with forwarded_from set to the original.
        """
        # Fetch original message
        orig_result = await db.execute(select(Message).where(Message.id == message_id))
        original = orig_result.scalar_one_or_none()
        if not original:
            raise NotFoundError("Message", message_id)

        # Verify user is a member of the original message's channel
        if not await ChannelService.is_member(db, original.channel_id, user_id):
            raise ForbiddenError("You are not a member of the source channel")

        # Verify sender is member of target channel
        if not await ChannelService.is_member(db, to_channel_id, user_id):
            raise ForbiddenError("You are not a member of the target channel")

        # Create forwarded message
        forwarded = Message(
            channel_id=to_channel_id,
            sender_id=user_id,
            content=original.content,
            type="text",  # Forwarded message is always text in new channel
            forwarded_from=message_id,
            file_id=original.file_id,  # Can forward file references
        )
        db.add(forwarded)
        await db.commit()

        # Wire receipts
        from app.services.sync_service import SyncService
        sync_svc = SyncService()
        await sync_svc.create_receipts_for_message(db, forwarded.id, user_id, to_channel_id)

        # Reload with relationships
        result = await db.execute(
            select(Message)
            .where(Message.id == forwarded.id)
            .options(
                selectinload(Message.sender),
                selectinload(Message.reactions),
            )
        )
        forwarded = result.scalar_one()
        logger.info("message_forwarded", original_id=message_id, forwarded_id=forwarded.id)
        return forwarded

    @staticmethod
    async def pin_message(
        db: AsyncSession,
        message_id: str,
        user_id: str,
    ) -> dict:
        """
        Pin a message to a channel.
        User must be a channel admin or moderator.
        """
        result = await db.execute(select(Message).where(Message.id == message_id))
        message = result.scalar_one_or_none()
        if not message:
            raise NotFoundError("Message", message_id)

        # Verify user is member of the channel
        if not await ChannelService.is_member(db, message.channel_id, user_id):
            raise ForbiddenError("You are not a member of this channel")

        # SECURITY: Only admin/moderator can pin messages
        try:
            await ChannelService._require_admin(db, message.channel_id, user_id)
        except ForbiddenError:
            raise ForbiddenError("Only channel admins and moderators can pin messages")

        message.pinned_at = datetime.now(timezone.utc)
        message.pinned_by = user_id
        await db.commit()
        logger.info("message_pinned", message_id=message_id, pinned_by=user_id)

        return {
            "message_id": message.id,
            "pinned_at": message.pinned_at.isoformat() if message.pinned_at else None,
            "pinned_by": message.pinned_by,
        }

    @staticmethod
    async def unpin_message(
        db: AsyncSession,
        message_id: str,
        user_id: str,
    ) -> dict:
        """
        Unpin a message from a channel.
        User must be a channel admin or moderator.
        """
        result = await db.execute(select(Message).where(Message.id == message_id))
        message = result.scalar_one_or_none()
        if not message:
            raise NotFoundError("Message", message_id)

        # Verify user is member of the channel
        if not await ChannelService.is_member(db, message.channel_id, user_id):
            raise ForbiddenError("You are not a member of this channel")

        # SECURITY: Only admin/moderator can unpin messages
        try:
            await ChannelService._require_admin(db, message.channel_id, user_id)
        except ForbiddenError:
            raise ForbiddenError("Only channel admins and moderators can unpin messages")

        message.pinned_at = None
        message.pinned_by = None
        await db.commit()
        logger.info("message_unpinned", message_id=message_id, unpinned_by=user_id)

        return {
            "message_id": message.id,
            "pinned_at": None,
            "pinned_by": None,
        }

    @staticmethod
    async def get_pinned_messages(
        db: AsyncSession,
        channel_id: str,
        user_id: str,
    ) -> list[Message]:
        """
        Fetch all pinned messages in a channel.
        User must be a channel member.
        """
        if not await ChannelService.is_member(db, channel_id, user_id):
            raise ForbiddenError("You are not a member of this channel")

        result = await db.execute(
            select(Message)
            .where(
                Message.channel_id == channel_id,
                Message.pinned_at.isnot(None),
                Message.deleted_at.is_(None),
            )
            .options(
                selectinload(Message.sender),
                selectinload(Message.reactions),
                selectinload(Message.pinned_by_user),
            )
            .order_by(Message.pinned_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def bulk_delete_messages(
        db: AsyncSession,
        message_ids: list[str],
        user_id: str,
    ) -> int:
        """
        Soft-delete multiple messages (mark deleted_at).
        All messages must belong to the user.
        Returns count of deleted messages.
        """
        now = datetime.now(timezone.utc)

        # Verify all messages belong to user
        result = await db.execute(
            select(func.count(Message.id)).where(
                Message.id.in_(message_ids),
                Message.sender_id == user_id,
            )
        )
        count_matching = result.scalar() or 0

        if count_matching != len(message_ids):
            raise ForbiddenError("You can only delete your own messages")

        stmt = (
            update(Message)
            .where(
                Message.id.in_(message_ids),
                Message.sender_id == user_id,
            )
            .values(deleted_at=now)
        )
        result = await db.execute(stmt)
        await db.commit()

        deleted_count = result.rowcount
        logger.info("bulk_deleted", user_id=user_id, count=deleted_count)
        return deleted_count

    @staticmethod
    async def get_message_count(
        db: AsyncSession,
        channel_id: str,
    ) -> int:
        """Get total message count for a channel (excluding deleted)."""
        result = await db.execute(
            select(func.count(Message.id)).where(
                Message.channel_id == channel_id,
                Message.deleted_at.is_(None),
            )
        )
        return result.scalar() or 0
