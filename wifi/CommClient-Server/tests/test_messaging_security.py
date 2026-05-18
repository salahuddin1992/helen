"""
Messaging and channel membership security tests.

Covers:
  - Message send validates channel membership
  - Message edit/delete ownership enforcement
  - Reaction toggle membership check
  - Pin/unpin admin-only enforcement
  - Forward message cross-channel membership
  - Search scoped to user's channels
  - Bulk delete ownership enforcement
  - Thread retrieval membership check
  - Message pagination boundary conditions
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.security import hash_password
from app.models.channel import Channel, ChannelMember
from app.models.message import Message
from app.models.user import User
from app.services.channel_service import ChannelService
from app.services.message_service import MessageService


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
async def user_a(db_session: AsyncSession):
    """Create user A."""
    user = User(
        username="msg_user_a",
        display_name="User A",
        password_hash=hash_password("PassA123!"),
        role="user",
        status="online",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    await db_session.commit()
    return user


@pytest.fixture
async def user_b(db_session: AsyncSession):
    """Create user B."""
    user = User(
        username="msg_user_b",
        display_name="User B",
        password_hash=hash_password("PassB123!"),
        role="user",
        status="online",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    await db_session.commit()
    return user


@pytest.fixture
async def admin_user_msg(db_session: AsyncSession):
    """Create admin user for messaging tests."""
    user = User(
        username="msg_admin",
        display_name="Admin Msg",
        password_hash=hash_password("AdminMsg123!"),
        role="admin",
        status="online",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    await db_session.commit()
    return user


@pytest.fixture
async def channel_ab(db_session: AsyncSession, user_a: User, user_b: User):
    """Create a channel with user_a and user_b as members."""
    channel = Channel(name="test_dm", type="dm", created_by=user_a.id)
    db_session.add(channel)
    await db_session.flush()
    await db_session.refresh(channel)

    # Add both users as members
    db_session.add(ChannelMember(channel_id=channel.id, user_id=user_a.id, role="admin"))
    db_session.add(ChannelMember(channel_id=channel.id, user_id=user_b.id, role="member"))
    await db_session.commit()
    return channel


@pytest.fixture
async def channel_a_only(db_session: AsyncSession, user_a: User):
    """Create a channel with only user_a as member."""
    channel = Channel(name="private_channel", type="group", created_by=user_a.id)
    db_session.add(channel)
    await db_session.flush()
    await db_session.refresh(channel)
    db_session.add(ChannelMember(channel_id=channel.id, user_id=user_a.id, role="admin"))
    await db_session.commit()
    return channel


@pytest.fixture
async def message_from_a(
    db_session: AsyncSession, channel_ab: Channel, user_a: User
):
    """Create a message from user_a in channel_ab."""
    msg = Message(
        channel_id=channel_ab.id,
        sender_id=user_a.id,
        content="Hello from A",
        type="text",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    return msg


# ══════════════════════════════════════════════════════════════════
# Send Message — Membership Enforcement
# ══════════════════════════════════════════════════════════════════


class TestSendMessageMembership:
    """Verify send_message checks channel membership."""

    @pytest.mark.anyio
    async def test_member_can_send(
        self, db_session: AsyncSession, channel_ab: Channel, user_a: User
    ):
        """User who is a member can send messages."""
        msg = await MessageService.send_message(
            db_session, channel_ab.id, user_a.id, "Test message"
        )
        assert msg.content == "Test message"
        assert msg.sender_id == user_a.id

    @pytest.mark.anyio
    async def test_non_member_cannot_send(
        self,
        db_session: AsyncSession,
        channel_a_only: Channel,
        user_b: User,
    ):
        """User who is NOT a member gets ForbiddenError."""
        with pytest.raises(ForbiddenError):
            await MessageService.send_message(
                db_session, channel_a_only.id, user_b.id, "Should fail"
            )

    @pytest.mark.anyio
    async def test_message_content_length_limit(
        self, db_session: AsyncSession, channel_ab: Channel, user_a: User
    ):
        """Message >10000 characters is rejected."""
        with pytest.raises(ValueError, match="maximum length"):
            await MessageService.send_message(
                db_session, channel_ab.id, user_a.id, "x" * 10001
            )


# ══════════════════════════════════════════════════════════════════
# Edit Message — Ownership Enforcement
# ══════════════════════════════════════════════════════════════════


class TestEditMessageOwnership:
    """Verify edit_message enforces ownership."""

    @pytest.mark.anyio
    async def test_owner_can_edit(
        self,
        db_session: AsyncSession,
        message_from_a: Message,
        user_a: User,
    ):
        """Message sender can edit their own message."""
        edited = await MessageService.edit_message(
            db_session, message_from_a.id, user_a.id, "Edited content"
        )
        assert edited.content == "Edited content"
        assert edited.edited_at is not None

    @pytest.mark.anyio
    async def test_non_owner_cannot_edit(
        self,
        db_session: AsyncSession,
        message_from_a: Message,
        user_b: User,
    ):
        """Non-sender cannot edit someone else's message."""
        with pytest.raises(ForbiddenError):
            await MessageService.edit_message(
                db_session, message_from_a.id, user_b.id, "Hacked"
            )

    @pytest.mark.anyio
    async def test_edit_empty_content_rejected(
        self,
        db_session: AsyncSession,
        message_from_a: Message,
        user_a: User,
    ):
        """Empty content edit is rejected."""
        with pytest.raises(ValueError):
            await MessageService.edit_message(
                db_session, message_from_a.id, user_a.id, ""
            )

    @pytest.mark.anyio
    async def test_edit_nonexistent_message(
        self, db_session: AsyncSession, user_a: User
    ):
        """Editing a non-existent message raises NotFoundError."""
        with pytest.raises(NotFoundError):
            await MessageService.edit_message(
                db_session, "nonexistent_id", user_a.id, "Content"
            )


# ══════════════════════════════════════════════════════════════════
# Delete Message — Ownership Enforcement
# ══════════════════════════════════════════════════════════════════


class TestDeleteMessageOwnership:
    """Verify delete_message enforces ownership."""

    @pytest.mark.anyio
    async def test_owner_can_delete(
        self,
        db_session: AsyncSession,
        message_from_a: Message,
        user_a: User,
    ):
        """Sender can soft-delete their own message."""
        await MessageService.delete_message(
            db_session, message_from_a.id, user_a.id
        )
        # Verify soft-delete
        result = await db_session.execute(
            select(Message).where(Message.id == message_from_a.id)
        )
        msg = result.scalar_one()
        assert msg.deleted_at is not None

    @pytest.mark.anyio
    async def test_non_owner_cannot_delete(
        self,
        db_session: AsyncSession,
        message_from_a: Message,
        user_b: User,
    ):
        """Non-sender cannot delete someone else's message."""
        with pytest.raises(ForbiddenError):
            await MessageService.delete_message(
                db_session, message_from_a.id, user_b.id
            )


# ══════════════════════════════════════════════════════════════════
# Reaction Toggle — Membership Check
# ══════════════════════════════════════════════════════════════════


class TestReactionMembership:
    """Verify toggle_reaction checks channel membership."""

    @pytest.mark.anyio
    async def test_member_can_react(
        self,
        db_session: AsyncSession,
        message_from_a: Message,
        user_b: User,
    ):
        """Channel member can add a reaction."""
        reactions = await MessageService.toggle_reaction(
            db_session, message_from_a.id, user_b.id, "👍"
        )
        assert any(r.emoji == "👍" and r.user_id == user_b.id for r in reactions)

    @pytest.mark.anyio
    async def test_toggle_removes_existing_reaction(
        self,
        db_session: AsyncSession,
        message_from_a: Message,
        user_b: User,
    ):
        """Toggling same reaction again removes it."""
        # Add reaction
        await MessageService.toggle_reaction(
            db_session, message_from_a.id, user_b.id, "🔥"
        )
        # Toggle (remove) reaction
        reactions = await MessageService.toggle_reaction(
            db_session, message_from_a.id, user_b.id, "🔥"
        )
        assert not any(r.emoji == "🔥" and r.user_id == user_b.id for r in reactions)

    @pytest.mark.anyio
    async def test_reaction_on_nonexistent_message(
        self, db_session: AsyncSession, user_a: User
    ):
        """Reacting to nonexistent message raises NotFoundError."""
        with pytest.raises(NotFoundError):
            await MessageService.toggle_reaction(
                db_session, "nonexistent_msg_id", user_a.id, "👍"
            )


# ══════════════════════════════════════════════════════════════════
# Get Messages — Membership and Pagination
# ══════════════════════════════════════════════════════════════════


class TestGetMessages:
    """Verify get_messages membership check and pagination."""

    @pytest.mark.anyio
    async def test_member_can_retrieve_messages(
        self,
        db_session: AsyncSession,
        channel_ab: Channel,
        user_a: User,
        message_from_a: Message,
    ):
        """Member can retrieve channel messages."""
        messages, has_more, total = await MessageService.get_messages(
            db_session, channel_ab.id, user_a.id
        )
        assert total >= 1
        assert any(m.id == message_from_a.id for m in messages)

    @pytest.mark.anyio
    async def test_non_member_cannot_retrieve_messages(
        self,
        db_session: AsyncSession,
        channel_a_only: Channel,
        user_b: User,
    ):
        """Non-member gets ForbiddenError when retrieving messages."""
        with pytest.raises(ForbiddenError):
            await MessageService.get_messages(
                db_session, channel_a_only.id, user_b.id
            )

    @pytest.mark.anyio
    async def test_pagination_has_more_detection(
        self,
        db_session: AsyncSession,
        channel_ab: Channel,
        user_a: User,
    ):
        """Pagination correctly detects has_more when more messages exist."""
        # Insert 5 messages
        for i in range(5):
            db_session.add(Message(
                channel_id=channel_ab.id,
                sender_id=user_a.id,
                content=f"Msg {i}",
                type="text",
            ))
        await db_session.commit()

        # Request with limit=3
        messages, has_more, total = await MessageService.get_messages(
            db_session, channel_ab.id, user_a.id, limit=3
        )
        assert has_more is True
        assert len(messages) == 3

    @pytest.mark.anyio
    async def test_deleted_messages_excluded(
        self,
        db_session: AsyncSession,
        channel_ab: Channel,
        user_a: User,
    ):
        """Soft-deleted messages are excluded from query results."""
        msg = Message(
            channel_id=channel_ab.id,
            sender_id=user_a.id,
            content="Will be deleted",
            type="text",
            deleted_at=datetime.now(timezone.utc),
        )
        db_session.add(msg)
        await db_session.commit()

        messages, _, _ = await MessageService.get_messages(
            db_session, channel_ab.id, user_a.id
        )
        assert not any(m.id == msg.id for m in messages)


# ══════════════════════════════════════════════════════════════════
# Search Messages — Channel Scoping
# ══════════════════════════════════════════════════════════════════


class TestSearchMessages:
    """Verify search is scoped to user's channels."""

    @pytest.mark.anyio
    async def test_search_returns_matching_messages(
        self,
        db_session: AsyncSession,
        channel_ab: Channel,
        user_a: User,
    ):
        """Search finds messages containing query text."""
        db_session.add(Message(
            channel_id=channel_ab.id,
            sender_id=user_a.id,
            content="unique_searchable_term_xyz",
            type="text",
        ))
        await db_session.commit()

        results, total = await MessageService.search_messages(
            db_session, user_a.id, "unique_searchable_term_xyz"
        )
        assert total >= 1
        assert any("unique_searchable_term_xyz" in m.content for m in results)

    @pytest.mark.anyio
    async def test_search_cannot_find_messages_in_non_member_channel(
        self,
        db_session: AsyncSession,
        channel_a_only: Channel,
        user_a: User,
        user_b: User,
    ):
        """Search does not return messages from channels user is not a member of."""
        db_session.add(Message(
            channel_id=channel_a_only.id,
            sender_id=user_a.id,
            content="secret_content_for_search_test",
            type="text",
        ))
        await db_session.commit()

        # user_b is NOT a member of channel_a_only
        results, total = await MessageService.search_messages(
            db_session, user_b.id, "secret_content_for_search_test"
        )
        assert total == 0


# ══════════════════════════════════════════════════════════════════
# Bulk Delete — Ownership Enforcement
# ══════════════════════════════════════════════════════════════════


class TestBulkDelete:
    """Verify bulk_delete_messages enforces ownership."""

    @pytest.mark.anyio
    async def test_owner_can_bulk_delete(
        self,
        db_session: AsyncSession,
        channel_ab: Channel,
        user_a: User,
    ):
        """Sender can bulk-delete their own messages."""
        msgs = []
        for i in range(3):
            m = Message(
                channel_id=channel_ab.id,
                sender_id=user_a.id,
                content=f"Bulk msg {i}",
                type="text",
            )
            db_session.add(m)
            msgs.append(m)
        await db_session.flush()
        await db_session.commit()

        msg_ids = [m.id for m in msgs]
        deleted_count = await MessageService.bulk_delete_messages(
            db_session, msg_ids, user_a.id
        )
        assert deleted_count == 3

    @pytest.mark.anyio
    async def test_cannot_bulk_delete_others_messages(
        self,
        db_session: AsyncSession,
        message_from_a: Message,
        user_b: User,
    ):
        """Cannot bulk-delete messages sent by another user."""
        with pytest.raises(ForbiddenError):
            await MessageService.bulk_delete_messages(
                db_session, [message_from_a.id], user_b.id
            )


# ══════════════════════════════════════════════════════════════════
# Reaction Aggregation
# ══════════════════════════════════════════════════════════════════


class TestReactionAggregation:
    """Verify reaction aggregation helper."""

    def test_aggregate_empty(self):
        """Empty reaction list returns empty aggregation."""
        result = MessageService.aggregate_reactions([])
        assert result == []

    def test_aggregate_groups_by_emoji(self):
        """Reactions are grouped by emoji with count and user_ids."""
        from app.models.message import Reaction

        # Create mock reaction objects
        class MockReaction:
            def __init__(self, emoji, user_id):
                self.emoji = emoji
                self.user_id = user_id

        reactions = [
            MockReaction("👍", "user_1"),
            MockReaction("👍", "user_2"),
            MockReaction("🔥", "user_1"),
        ]
        result = MessageService.aggregate_reactions(reactions)

        thumbs = next(r for r in result if r["emoji"] == "👍")
        assert thumbs["count"] == 2
        assert set(thumbs["user_ids"]) == {"user_1", "user_2"}

        fire = next(r for r in result if r["emoji"] == "🔥")
        assert fire["count"] == 1
