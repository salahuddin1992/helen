"""
User endpoint tests.

Covers user profile retrieval, updates, listing, and contact management.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.core.security import hash_password


class TestUserProfile:
    """Tests for user profile endpoints."""

    async def test_get_current_user_success(
        self, client: AsyncClient, auth_headers: dict, test_user
    ):
        """GET /api/users/me with valid token returns user profile."""
        response = await client.get("/api/users/me", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == test_user.id
        assert data["username"] == test_user.username
        assert data["display_name"] == test_user.display_name
        assert "status" in data
        assert "last_seen" in data
        assert "created_at" in data

    async def test_get_current_user_without_auth(self, client: AsyncClient):
        """GET /api/users/me without token returns 403."""
        response = await client.get("/api/users/me")

        assert response.status_code == 403

    async def test_get_current_user_invalid_token(self, client: AsyncClient):
        """GET /api/users/me with invalid token returns 401."""
        response = await client.get(
            "/api/users/me",
            headers={"Authorization": "Bearer invalid.token.here"},
        )

        assert response.status_code == 401

    async def test_update_current_user_display_name(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PATCH /api/users/me updates display_name."""
        response = await client.patch(
            "/api/users/me",
            json={"display_name": "Updated Name"},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["display_name"] == "Updated Name"

    async def test_update_current_user_avatar(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PATCH /api/users/me updates avatar_url."""
        new_avatar = "https://example.com/new-avatar.jpg"
        response = await client.patch(
            "/api/users/me",
            json={"avatar_url": new_avatar},
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["avatar_url"] == new_avatar

    async def test_update_current_user_bio(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PATCH /api/users/me updates bio."""
        new_bio = "New bio text"
        response = await client.patch(
            "/api/users/me",
            json={"bio": new_bio},
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["bio"] == new_bio

    async def test_update_current_user_status(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PATCH /api/users/me updates status (online/away/busy/dnd)."""
        for status in ["online", "away", "busy", "dnd"]:
            response = await client.patch(
                "/api/users/me",
                json={"status": status},
                headers=auth_headers,
            )
            assert response.status_code == 200
            assert response.json()["status"] == status

    async def test_update_current_user_invalid_status(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PATCH /api/users/me with invalid status returns 422."""
        response = await client.patch(
            "/api/users/me",
            json={"status": "invalid_status"},
            headers=auth_headers,
        )

        assert response.status_code == 422

    async def test_update_current_user_multiple_fields(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PATCH /api/users/me can update multiple fields at once."""
        response = await client.patch(
            "/api/users/me",
            json={
                "display_name": "New Name",
                "bio": "New bio",
                "status": "away",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["display_name"] == "New Name"
        assert data["bio"] == "New bio"
        assert data["status"] == "away"

    async def test_update_current_user_without_auth(self, client: AsyncClient):
        """PATCH /api/users/me without token returns 403."""
        response = await client.patch(
            "/api/users/me",
            json={"display_name": "New Name"},
        )

        assert response.status_code == 403


class TestUserListing:
    """Tests for user list endpoint."""

    async def test_list_users_default(
        self, client: AsyncClient, auth_headers: dict, db_session: AsyncSession
    ):
        """GET /api/users returns all users with default pagination."""
        # Create a few users
        for i in range(3):
            user = User(
                username=f"user{i}",
                display_name=f"User {i}",
                password_hash=hash_password("pass123"),
                status="online",
            )
            db_session.add(user)
        await db_session.commit()

        response = await client.get("/api/users", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert "users" in data
        assert "total" in data
        assert isinstance(data["users"], list)
        assert data["total"] >= 3

    async def test_list_users_pagination(
        self, client: AsyncClient, auth_headers: dict, db_session: AsyncSession
    ):
        """GET /api/users with skip and limit parameters."""
        # Create 10 users
        for i in range(10):
            user = User(
                username=f"paginationuser{i}",
                display_name=f"Pagination User {i}",
                password_hash=hash_password("pass123"),
                status="online",
            )
            db_session.add(user)
        await db_session.commit()

        # Test skip
        response = await client.get(
            "/api/users?skip=2&limit=5",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["users"]) <= 5

    async def test_list_users_search(
        self, client: AsyncClient, auth_headers: dict, db_session: AsyncSession
    ):
        """GET /api/users with search parameter filters results."""
        # Create users with different names
        user1 = User(
            username="alice",
            display_name="Alice Wonder",
            password_hash=hash_password("pass123"),
            status="online",
        )
        user2 = User(
            username="bob",
            display_name="Bob Smith",
            password_hash=hash_password("pass123"),
            status="online",
        )
        db_session.add_all([user1, user2])
        await db_session.commit()

        response = await client.get(
            "/api/users?search=alice",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        # Should find Alice
        usernames = [u["username"] for u in data["users"]]
        assert "alice" in usernames

    async def test_list_users_without_auth(self, client: AsyncClient):
        """GET /api/users without token returns 403."""
        response = await client.get("/api/users")
        assert response.status_code == 403


class TestGetUserById:
    """Tests for getting a specific user by ID."""

    async def test_get_user_by_id(
        self, client: AsyncClient, auth_headers: dict, second_user
    ):
        """GET /api/users/{id} returns user profile."""
        response = await client.get(
            f"/api/users/{second_user.id}",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == second_user.id
        assert data["username"] == second_user.username
        assert data["display_name"] == second_user.display_name

    async def test_get_user_by_id_nonexistent(
        self, client: AsyncClient, auth_headers: dict
    ):
        """GET /api/users/{nonexistent_id} returns 404."""
        response = await client.get(
            "/api/users/nonexistent_id_here",
            headers=auth_headers,
        )

        assert response.status_code == 404

    async def test_get_user_by_id_without_auth(
        self, client: AsyncClient, second_user
    ):
        """GET /api/users/{id} without token returns 403."""
        response = await client.get(f"/api/users/{second_user.id}")
        assert response.status_code == 403


class TestContacts:
    """Tests for contact management endpoints."""

    async def test_list_contacts_empty(
        self, client: AsyncClient, auth_headers: dict
    ):
        """GET /api/users/me/contacts with no contacts returns empty list."""
        response = await client.get(
            "/api/users/me/contacts",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    async def test_add_contact_success(
        self,
        client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        test_user,
        second_user,
    ):
        """POST /api/users/me/contacts adds a contact."""
        response = await client.post(
            "/api/users/me/contacts",
            json={
                "contact_id": second_user.id,
                "nickname": "My friend",
            },
            headers=auth_headers,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] is not None
        assert data["contact"]["id"] == second_user.id
        assert data["nickname"] == "My friend"
        assert data["is_blocked"] == False
        assert data["is_favorite"] == False

    async def test_add_contact_without_nickname(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """POST /api/users/me/contacts works without nickname."""
        response = await client.post(
            "/api/users/me/contacts",
            json={
                "contact_id": second_user.id,
            },
            headers=auth_headers,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["contact"]["id"] == second_user.id
        assert data["nickname"] is None

    async def test_list_contacts_after_add(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """GET /api/users/me/contacts returns added contacts."""
        # Add contact
        await client.post(
            "/api/users/me/contacts",
            json={
                "contact_id": second_user.id,
                "nickname": "Friend",
            },
            headers=auth_headers,
        )

        # List contacts
        response = await client.get(
            "/api/users/me/contacts",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["contact"]["id"] == second_user.id

    async def test_update_contact(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """PATCH /api/users/me/contacts/{id} updates contact fields."""
        # Add contact
        add_resp = await client.post(
            "/api/users/me/contacts",
            json={
                "contact_id": second_user.id,
                "nickname": "Old Name",
            },
            headers=auth_headers,
        )
        contact_id = add_resp.json()["id"]

        # Update contact
        response = await client.patch(
            f"/api/users/me/contacts/{contact_id}",
            json={
                "nickname": "New Name",
                "is_favorite": True,
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        assert response.json()["status"] == "updated"

    async def test_update_contact_block(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """PATCH /api/users/me/contacts/{id} can block contact."""
        add_resp = await client.post(
            "/api/users/me/contacts",
            json={"contact_id": second_user.id},
            headers=auth_headers,
        )
        contact_id = add_resp.json()["id"]

        # Block contact
        await client.patch(
            f"/api/users/me/contacts/{contact_id}",
            json={"is_blocked": True},
            headers=auth_headers,
        )

        # Verify blocked
        list_resp = await client.get(
            "/api/users/me/contacts",
            headers=auth_headers,
        )
        assert list_resp.json()[0]["is_blocked"] == True

    async def test_remove_contact(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_user,
    ):
        """DELETE /api/users/me/contacts/{id} removes contact."""
        add_resp = await client.post(
            "/api/users/me/contacts",
            json={"contact_id": second_user.id},
            headers=auth_headers,
        )
        contact_id = add_resp.json()["id"]

        # Remove contact
        response = await client.delete(
            f"/api/users/me/contacts/{contact_id}",
            headers=auth_headers,
        )
        assert response.status_code == 204

        # Verify removed
        list_resp = await client.get(
            "/api/users/me/contacts",
            headers=auth_headers,
        )
        assert len(list_resp.json()) == 0

    async def test_add_contact_without_auth(
        self, client: AsyncClient, second_user
    ):
        """POST /api/users/me/contacts without auth returns 403."""
        response = await client.post(
            "/api/users/me/contacts",
            json={"contact_id": second_user.id},
        )
        assert response.status_code == 403
