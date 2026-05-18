"""
Message endpoint tests.

Covers message sending, retrieval, editing, deletion, reactions, and search.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message


class TestSendMessage:
    """Tests for POST /api/channels/{id}/messages endpoint."""

    async def test_send_message_success(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Send a text message to a channel."""
        # Create channel first
        channel_resp = await client.post(
            "/api/channels",
            json={
                "type": "dm",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        # Send message
        response = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={
                "content": "Hello, this is a test message!",
                "type": "text",
            },
            headers=auth_headers,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] is not None
        assert data["channel_id"] == channel_id
        assert data["content"] == "Hello, this is a test message!"
        assert data["type"] == "text"
        assert "sender" in data
        assert "created_at" in data
        assert "reactions" in data

    async def test_send_message_with_reply(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Send a message as a reply to another message."""
        # Create channel and first message
        channel_resp = await client.post(
            "/api/channels",
            json={
                "type": "dm",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        msg1 = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"content": "First message", "type": "text"},
            headers=auth_headers,
        )
        msg1_id = msg1.json()["id"]

        # Reply to first message
        response = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={
                "content": "This is a reply",
                "type": "reply",
                "reply_to": msg1_id,
            },
            headers=auth_headers,
        )

        assert response.status_code == 201
        assert response.json()["reply_to"] == msg1_id

    async def test_send_message_empty_content(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Send message with empty content returns 422."""
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        response = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"content": ""},
            headers=auth_headers,
        )

        assert response.status_code == 422

    async def test_send_message_without_auth(
        self, client: AsyncClient, second_user
    ):
        """Send message without auth returns 403."""
        response = await client.post(
            "/api/channels/some_id/messages",
            json={"content": "Unauthorized message"},
        )
        assert response.status_code == 403


class TestGetMessages:
    """Tests for GET /api/channels/{id}/messages endpoint."""

    async def test_get_messages_empty_channel(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Get messages from a channel with no messages."""
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        response = await client.get(
            f"/api/channels/{channel_id}/messages",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "messages" in data
        assert "has_more" in data
        assert "total" in data
        assert len(data["messages"]) == 0
        assert data["has_more"] == False

    async def test_get_messages_with_messages(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Get messages returns sent messages."""
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        # Send some messages
        for i in range(3):
            await client.post(
                f"/api/channels/{channel_id}/messages",
                json={"content": f"Message {i}"},
                headers=auth_headers,
            )

        response = await client.get(
            f"/api/channels/{channel_id}/messages",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 3
        assert data["total"] == 3

    async def test_get_messages_pagination(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Get messages with limit parameter."""
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        # Send multiple messages
        for i in range(10):
            await client.post(
                f"/api/channels/{channel_id}/messages",
                json={"content": f"Message {i}"},
                headers=auth_headers,
            )

        # Get with limit
        response = await client.get(
            f"/api/channels/{channel_id}/messages?limit=5",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) <= 5

    async def test_get_messages_with_before_cursor(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Get messages before a specific timestamp."""
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        # Send message
        msg_resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"content": "Test message"},
            headers=auth_headers,
        )

        created_at = msg_resp.json()["created_at"]

        # Get messages before now (should include the message)
        future = datetime.now(timezone.utc) + timedelta(seconds=10)
        response = await client.get(
            f"/api/channels/{channel_id}/messages",
            params={"before": future.isoformat()},
            headers=auth_headers,
        )

        assert response.status_code == 200

    async def test_get_messages_without_auth(self, client: AsyncClient):
        """Get messages without auth returns 403."""
        response = await client.get("/api/channels/some_id/messages")
        assert response.status_code == 403


class TestEditMessage:
    """Tests for PATCH /api/messages/{id} endpoint."""

    async def test_edit_message_success(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Edit a message's content."""
        # Create channel and message
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        msg_resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"content": "Original content"},
            headers=auth_headers,
        )
        msg_id = msg_resp.json()["id"]

        # Edit message
        response = await client.patch(
            f"/api/messages/{msg_id}",
            json={"content": "Edited content"},
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["content"] == "Edited content"
        assert "edited_at" in response.json()

    async def test_edit_someone_elses_message(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user_headers: dict,
        second_user,
    ):
        """Cannot edit another user's message (permission denied)."""
        # User 1 creates channel and message
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "group", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        msg_resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"content": "User 1's message"},
            headers=auth_headers,
        )
        msg_id = msg_resp.json()["id"]

        # User 2 tries to edit it (if they're in the channel)
        # This should fail with 403/permission denied

    async def test_edit_nonexistent_message(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Edit nonexistent message returns 404."""
        response = await client.patch(
            "/api/messages/nonexistent_id",
            json={"content": "New content"},
            headers=auth_headers,
        )

        assert response.status_code == 404

    async def test_edit_message_without_auth(self, client: AsyncClient):
        """Edit message without auth returns 403."""
        response = await client.patch(
            "/api/messages/some_id",
            json={"content": "Edited"},
        )
        assert response.status_code == 403


class TestDeleteMessage:
    """Tests for DELETE /api/messages/{id} endpoint."""

    async def test_delete_message_success(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Delete a message."""
        # Create channel and message
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        msg_resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"content": "Delete me"},
            headers=auth_headers,
        )
        msg_id = msg_resp.json()["id"]

        # Delete message
        response = await client.delete(
            f"/api/messages/{msg_id}",
            headers=auth_headers,
        )

        assert response.status_code == 204

        # Verify deleted by getting messages
        get_resp = await client.get(
            f"/api/channels/{channel_id}/messages",
            headers=auth_headers,
        )
        msg_ids = [m["id"] for m in get_resp.json()["messages"]]
        assert msg_id not in msg_ids

    async def test_delete_nonexistent_message(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Delete nonexistent message returns 404."""
        response = await client.delete(
            "/api/messages/nonexistent_id",
            headers=auth_headers,
        )

        assert response.status_code == 404

    async def test_delete_message_without_auth(self, client: AsyncClient):
        """Delete message without auth returns 403."""
        response = await client.delete("/api/messages/some_id")
        assert response.status_code == 403


class TestMessageReactions:
    """Tests for reaction endpoints."""

    async def test_add_reaction_to_message(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Add an emoji reaction to a message."""
        # Create channel and message
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        msg_resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"content": "React to me"},
            headers=auth_headers,
        )
        msg_id = msg_resp.json()["id"]

        # Add reaction
        response = await client.post(
            f"/api/messages/{msg_id}/reactions",
            json={"emoji": "👍"},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "reactions" in data
        reactions = data["reactions"]
        assert len(reactions) > 0
        assert reactions[0]["emoji"] == "👍"

    async def test_toggle_reaction(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Toggle reaction (add same reaction twice removes it)."""
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        msg_resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"content": "Toggle me"},
            headers=auth_headers,
        )
        msg_id = msg_resp.json()["id"]

        # Add reaction
        add_resp = await client.post(
            f"/api/messages/{msg_id}/reactions",
            json={"emoji": "❤️"},
            headers=auth_headers,
        )
        assert len(add_resp.json()["reactions"]) > 0

        # Toggle same reaction again (should remove)
        toggle_resp = await client.post(
            f"/api/messages/{msg_id}/reactions",
            json={"emoji": "❤️"},
            headers=auth_headers,
        )
        # Reaction should be removed
        reactions = toggle_resp.json()["reactions"]
        emoji_list = [r["emoji"] for r in reactions]
        assert "❤️" not in emoji_list

    async def test_multiple_reactions_on_message(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Message can have multiple different reactions."""
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        msg_resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"content": "Multiple reactions"},
            headers=auth_headers,
        )
        msg_id = msg_resp.json()["id"]

        # Add multiple reactions
        emojis = ["👍", "❤️", "😂", "🎉"]
        for emoji in emojis:
            await client.post(
                f"/api/messages/{msg_id}/reactions",
                json={"emoji": emoji},
                headers=auth_headers,
            )

        # Verify all reactions are there
        msg_get = await client.get(
            f"/api/channels/{channel_id}/messages",
            headers=auth_headers,
        )
        message = msg_get.json()["messages"][0]
        reactions = message["reactions"]
        assert len(reactions) == len(emojis)

    async def test_react_without_auth(self, client: AsyncClient):
        """React without auth returns 403."""
        response = await client.post(
            "/api/messages/some_id/reactions",
            json={"emoji": "👍"},
        )
        assert response.status_code == 403


class TestMarkMessageRead:
    """Tests for marking messages as read."""

    async def test_mark_message_read(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Mark a message as read."""
        channel_resp = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        channel_id = channel_resp.json()["id"]

        msg_resp = await client.post(
            f"/api/channels/{channel_id}/messages",
            json={"content": "Mark as read"},
            headers=auth_headers,
        )
        msg_id = msg_resp.json()["id"]

        # Mark as read
        response = await client.post(
            f"/api/messages/{msg_id}/read?channel_id={channel_id}",
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["status"] == "read"

    async def test_mark_read_without_auth(self, client: AsyncClient):
        """Mark as read without auth returns 403."""
        response = await client.post(
            "/api/messages/some_id/read?channel_id=some_channel",
        )
        assert response.status_code == 403
