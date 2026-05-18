"""
Channel endpoint tests.

Covers channel creation, updates, member management, and listing.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import Channel, ChannelMember


class TestCreateChannel:
    """Tests for POST /api/channels endpoint."""

    async def test_create_dm_channel(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Create a direct message channel (dm type)."""
        response = await client.post(
            "/api/channels",
            json={
                "type": "dm",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["type"] == "dm"
        assert data["id"] is not None
        assert "members" in data
        assert len(data["members"]) >= 1

    async def test_create_group_channel(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
    ):
        """Create a group channel with name and description."""
        from app.models.user import User
        from app.core.security import hash_password

        other_user = User(
            username="otheruser",
            display_name="Other User",
            password_hash=hash_password("pass123"),
            status="online",
        )
        db_session.add(other_user)
        await db_session.commit()

        response = await client.post(
            "/api/channels",
            json={
                "type": "group",
                "name": "Project Alpha",
                "description": "Discussion about Project Alpha",
                "member_ids": [other_user.id],
            },
            headers=auth_headers,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["type"] == "group"
        assert data["name"] == "Project Alpha"
        assert data["description"] == "Discussion about Project Alpha"
        assert data["is_active"] == True

    async def test_create_channel_without_auth(self, client: AsyncClient):
        """Create channel without token returns 403."""
        response = await client.post(
            "/api/channels",
            json={
                "type": "group",
                "name": "Public Channel",
            },
        )

        assert response.status_code == 403

    async def test_create_channel_invalid_type(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Create channel with invalid type returns 422."""
        response = await client.post(
            "/api/channels",
            json={
                "type": "invalid_type",
                "name": "Bad Channel",
            },
            headers=auth_headers,
        )

        assert response.status_code == 422

    async def test_create_dm_with_multiple_members(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
    ):
        """Create DM channel with multiple members."""
        from app.models.user import User
        from app.core.security import hash_password

        user1 = User(
            username="user1",
            display_name="User 1",
            password_hash=hash_password("pass123"),
            status="online",
        )
        user2 = User(
            username="user2",
            display_name="User 2",
            password_hash=hash_password("pass123"),
            status="online",
        )
        db_session.add_all([user1, user2])
        await db_session.commit()

        response = await client.post(
            "/api/channels",
            json={
                "type": "dm",
                "member_ids": [user1.id, user2.id],
            },
            headers=auth_headers,
        )

        # DM requires exactly one other member; passing two should be rejected
        assert response.status_code == 409


class TestListChannels:
    """Tests for GET /api/channels endpoint."""

    async def test_list_channels_empty(
        self, client: AsyncClient, auth_headers: dict
    ):
        """List channels when user has none returns empty list."""
        response = await client.get("/api/channels", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert "channels" in data
        assert "total" in data
        assert data["total"] >= 0

    async def test_list_channels_after_create(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """List channels returns created channels."""
        # Create a channel
        await client.post(
            "/api/channels",
            json={
                "type": "dm",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )

        # List channels
        response = await client.get("/api/channels", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1
        assert len(data["channels"]) >= 1

    async def test_list_channels_without_auth(self, client: AsyncClient):
        """List channels without token returns 403."""
        response = await client.get("/api/channels")
        assert response.status_code == 403


class TestGetChannel:
    """Tests for GET /api/channels/{id} endpoint."""

    async def test_get_channel_by_id(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Get a specific channel by ID."""
        # Create channel
        create_resp = await client.post(
            "/api/channels",
            json={
                "type": "dm",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )
        channel_id = create_resp.json()["id"]

        # Get channel
        response = await client.get(
            f"/api/channels/{channel_id}",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == channel_id
        assert data["type"] == "dm"

    async def test_get_nonexistent_channel(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Get nonexistent channel returns 404."""
        response = await client.get(
            "/api/channels/nonexistent_id",
            headers=auth_headers,
        )

        assert response.status_code == 404

    async def test_get_channel_without_auth(
        self, client: AsyncClient, second_user
    ):
        """Get channel without token returns 403."""
        # We need a channel ID that exists, but since we can't create without auth,
        # any request without auth will fail
        response = await client.get("/api/channels/some_id")
        assert response.status_code == 403


class TestUpdateChannel:
    """Tests for PATCH /api/channels/{id} endpoint."""

    async def test_update_channel_name(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Update channel name."""
        # Create group channel
        create_resp = await client.post(
            "/api/channels",
            json={
                "type": "group",
                "name": "Old Name",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )
        channel_id = create_resp.json()["id"]

        # Update
        response = await client.patch(
            f"/api/channels/{channel_id}",
            json={"name": "New Name"},
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["name"] == "New Name"

    async def test_update_channel_description(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Update channel description."""
        create_resp = await client.post(
            "/api/channels",
            json={
                "type": "group",
                "name": "Test Channel",
                "description": "Old description",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )
        channel_id = create_resp.json()["id"]

        response = await client.patch(
            f"/api/channels/{channel_id}",
            json={"description": "New description"},
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["description"] == "New description"

    async def test_update_channel_avatar(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Update channel avatar URL."""
        create_resp = await client.post(
            "/api/channels",
            json={
                "type": "group",
                "name": "Test Channel",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )
        channel_id = create_resp.json()["id"]

        new_avatar = "https://example.com/channel-avatar.jpg"
        response = await client.patch(
            f"/api/channels/{channel_id}",
            json={"avatar_url": new_avatar},
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["avatar_url"] == new_avatar

    async def test_update_channel_without_auth(
        self, client: AsyncClient, second_user
    ):
        """Update channel without auth returns 403."""
        response = await client.patch(
            "/api/channels/some_id",
            json={"name": "New Name"},
        )
        assert response.status_code == 403


class TestChannelMembers:
    """Tests for channel member management."""

    async def test_add_member_to_channel(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
    ):
        """Add a member to an existing channel."""
        from app.models.user import User
        from app.core.security import hash_password

        user1 = User(
            username="member1",
            display_name="Member 1",
            password_hash=hash_password("pass123"),
            status="online",
        )
        user2 = User(
            username="member2",
            display_name="Member 2",
            password_hash=hash_password("pass123"),
            status="online",
        )
        db_session.add_all([user1, user2])
        await db_session.commit()

        # Create channel with user1
        create_resp = await client.post(
            "/api/channels",
            json={
                "type": "group",
                "name": "Test Channel",
                "member_ids": [user1.id],
            },
            headers=auth_headers,
        )
        channel_id = create_resp.json()["id"]

        # Add user2
        response = await client.post(
            f"/api/channels/{channel_id}/members",
            json={"user_id": user2.id},
            headers=auth_headers,
        )

        assert response.status_code == 201
        assert response.json()["status"] == "member_added"

    async def test_add_member_with_role(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
    ):
        """Add member with specific role."""
        from app.models.user import User
        from app.core.security import hash_password

        user = User(
            username="moderator",
            display_name="Moderator",
            password_hash=hash_password("pass123"),
            status="online",
        )
        db_session.add(user)
        await db_session.commit()

        create_resp = await client.post(
            "/api/channels",
            json={
                "type": "group",
                "name": "Test Channel",
            },
            headers=auth_headers,
        )
        channel_id = create_resp.json()["id"]

        response = await client.post(
            f"/api/channels/{channel_id}/members",
            json={
                "user_id": user.id,
                "role": "moderator",
            },
            headers=auth_headers,
        )

        assert response.status_code == 201

    async def test_remove_member_from_channel(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Remove a member from a channel."""
        # Create channel with second user
        create_resp = await client.post(
            "/api/channels",
            json={
                "type": "group",
                "name": "Test Channel",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )
        channel_id = create_resp.json()["id"]

        # Remove member
        response = await client.delete(
            f"/api/channels/{channel_id}/members/{second_user.id}",
            headers=auth_headers,
        )

        assert response.status_code == 204

    async def test_remove_nonexistent_member(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Remove nonexistent member returns error."""
        create_resp = await client.post(
            "/api/channels",
            json={
                "type": "group",
                "name": "Test Channel",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )
        channel_id = create_resp.json()["id"]

        response = await client.delete(
            f"/api/channels/{channel_id}/members/nonexistent_id",
            headers=auth_headers,
        )

        assert response.status_code == 404

    async def test_add_member_without_auth(
        self, client: AsyncClient, second_user
    ):
        """Add member without auth returns 403."""
        response = await client.post(
            "/api/channels/some_id/members",
            json={"user_id": second_user.id},
        )
        assert response.status_code == 403


class TestChannelStructure:
    """Tests for channel response structure and fields."""

    async def test_channel_response_includes_all_fields(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """Channel response includes all expected fields."""
        response = await client.post(
            "/api/channels",
            json={
                "type": "group",
                "name": "Complete Channel",
                "description": "A complete channel",
                "member_ids": [second_user.id],
            },
            headers=auth_headers,
        )

        data = response.json()

        # Required fields
        assert "id" in data
        assert "type" in data
        assert "name" in data
        assert "description" in data
        assert "created_by" in data
        assert "is_active" in data
        assert "members" in data
        assert "member_count" in data
        assert "created_at" in data
        assert "updated_at" in data

        # Member structure
        members = data["members"]
        if len(members) > 0:
            member = members[0]
            assert "user_id" in member
            assert "username" in member
            assert "display_name" in member
            assert "status" in member
            assert "role" in member
            assert "joined_at" in member


class TestDeleteChannel:
    """
    Regression tests for DELETE /api/channels/{id}. Added after the
    delete-channel feature shipped — covers the auth matrix
    (creator/site-admin/DM-member/random-user) and the 404 path.
    """

    async def test_creator_can_delete_own_group(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """The user who created a group can delete it."""
        create = await client.post(
            "/api/channels",
            json={"type": "group", "name": "DeleteMe", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        assert create.status_code == 201
        cid = create.json()["id"]

        delete = await client.delete(f"/api/channels/{cid}", headers=auth_headers)
        assert delete.status_code == 204

        # Verify gone
        get = await client.get(f"/api/channels/{cid}", headers=auth_headers)
        assert get.status_code == 404

    async def test_non_creator_cannot_delete_group(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user_headers: dict,
        second_user,
    ):
        """A regular member (not creator, not admin) gets 403 on DELETE."""
        create = await client.post(
            "/api/channels",
            json={"type": "group", "name": "OwnerGroup", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        assert create.status_code == 201
        cid = create.json()["id"]

        # second_user is a member but NOT the creator
        delete = await client.delete(f"/api/channels/{cid}", headers=second_user_headers)
        assert delete.status_code == 403

    async def test_dm_member_can_delete_dm(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user_headers: dict,
        second_user,
    ):
        """DM channels can be deleted by either participant."""
        create = await client.post(
            "/api/channels",
            json={"type": "dm", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        assert create.status_code == 201
        cid = create.json()["id"]

        # The OTHER participant deletes it — should succeed (not creator,
        # but DM members both have authority).
        delete = await client.delete(f"/api/channels/{cid}", headers=second_user_headers)
        assert delete.status_code == 204

    async def test_delete_nonexistent_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """DELETE on a channel id that doesn't exist returns 404."""
        delete = await client.delete(
            "/api/channels/nonexistent-channel-id", headers=auth_headers,
        )
        assert delete.status_code == 404

    async def test_delete_without_auth_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """DELETE without a Bearer token gets 403 (FastAPI default)."""
        create = await client.post(
            "/api/channels",
            json={"type": "group", "name": "AuthTest", "member_ids": [second_user.id]},
            headers=auth_headers,
        )
        cid = create.json()["id"]
        delete = await client.delete(f"/api/channels/{cid}")
        assert delete.status_code in (401, 403)
