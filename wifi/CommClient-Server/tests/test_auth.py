"""
Authentication endpoint tests.

Covers registration, login, refresh tokens, and logout with proper
validation of request/response formats and error conditions.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.models.user import User


class TestRegister:
    """Tests for POST /api/auth/register endpoint."""

    async def test_register_success(self, client: AsyncClient):
        """Register with valid data returns 201 with user and tokens."""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "newuser",
                "display_name": "New User",
                "password": "SecurePass123!",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert "user" in data
        assert "tokens" in data

        # Verify user fields
        user = data["user"]
        assert user["username"] == "newuser"
        assert user["display_name"] == "New User"
        assert user["id"] is not None
        assert "avatar_url" in user
        assert "status" in user

        # Verify token fields
        tokens = data["tokens"]
        assert "access_token" in tokens
        assert "refresh_token" in tokens
        assert tokens["token_type"] == "bearer"
        assert tokens["expires_in"] > 0

    async def test_register_with_avatar_and_bio(self, client: AsyncClient):
        """Register with optional avatar_url and bio fields."""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "userwitavatar",
                "display_name": "Avatar User",
                "password": "SecurePass123!",
                "avatar_url": "https://example.com/avatar.jpg",
                "bio": "This is my bio",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["user"]["avatar_url"] == "https://example.com/avatar.jpg"

    async def test_register_duplicate_username(
        self, client: AsyncClient, db_session: AsyncSession, test_user
    ):
        """Register with existing username returns 409 Conflict."""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": test_user.username,
                "display_name": "Different User",
                "password": "SecurePass123!",
            },
        )

        assert response.status_code == 409
        assert "already taken" in response.json()["detail"]

    async def test_register_weak_password(self, client: AsyncClient):
        """Register with weak password (< 6 chars) returns 422 (Pydantic validation)."""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "weakpass",
                "display_name": "Weak Password User",
                "password": "123",
            },
        )

        assert response.status_code == 422
        # Password validation error — Pydantic returns 422 Unprocessable Entity

    async def test_register_short_username(self, client: AsyncClient):
        """Register with username < 3 chars returns 422."""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "ab",
                "display_name": "Short Username",
                "password": "SecurePass123!",
            },
        )

        assert response.status_code == 422

    async def test_register_long_username(self, client: AsyncClient):
        """Register with username > 64 chars returns 422."""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "a" * 65,
                "display_name": "Long Username",
                "password": "SecurePass123!",
            },
        )

        assert response.status_code == 422

    async def test_register_invalid_username_chars(self, client: AsyncClient):
        """Register with invalid characters in username returns 422 (Pydantic pattern validation)."""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "user@name!",
                "display_name": "Invalid Chars",
                "password": "SecurePass123!",
            },
        )

        assert response.status_code == 422

    async def test_register_valid_username_chars(self, client: AsyncClient):
        """Register with valid username characters (alphanumeric, _, -, .)."""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "user_name.with-chars",
                "display_name": "Valid Chars User",
                "password": "SecurePass123!",
            },
        )

        assert response.status_code == 201
        assert response.json()["user"]["username"] == "user_name.with-chars"

    async def test_register_missing_display_name(self, client: AsyncClient):
        """Register without display_name returns 422."""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "noname",
                "password": "SecurePass123!",
            },
        )

        assert response.status_code == 422

    async def test_register_missing_password(self, client: AsyncClient):
        """Register without password returns 422."""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "nopass",
                "display_name": "No Password User",
            },
        )

        assert response.status_code == 422


class TestLogin:
    """Tests for POST /api/auth/login endpoint."""

    async def test_login_success(
        self, client: AsyncClient, db_session: AsyncSession, test_user_data
    ):
        """Login with correct credentials returns 200 with tokens."""
        # Register user first
        register_resp = await client.post(
            "/api/auth/register",
            json=test_user_data,
        )
        assert register_resp.status_code == 201

        # Now login
        response = await client.post(
            "/api/auth/login",
            json={
                "username": test_user_data["username"],
                "password": test_user_data["password"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "user" in data
        assert "tokens" in data
        assert data["user"]["username"] == test_user_data["username"]
        assert "access_token" in data["tokens"]
        assert "refresh_token" in data["tokens"]

    async def test_login_with_device_name(
        self, client: AsyncClient, test_user_data
    ):
        """Login with optional device_name field."""
        await client.post(
            "/api/auth/register",
            json=test_user_data,
        )

        response = await client.post(
            "/api/auth/login",
            json={
                "username": test_user_data["username"],
                "password": test_user_data["password"],
                "device_name": "MyLaptop",
            },
        )

        assert response.status_code == 200

    async def test_login_wrong_password(
        self, client: AsyncClient, test_user_data
    ):
        """Login with wrong password returns 401."""
        await client.post(
            "/api/auth/register",
            json=test_user_data,
        )

        response = await client.post(
            "/api/auth/login",
            json={
                "username": test_user_data["username"],
                "password": "WrongPassword123!",
            },
        )

        assert response.status_code == 401
        assert "Invalid username or password" in response.json()["detail"]

    async def test_login_nonexistent_user(self, client: AsyncClient):
        """Login with unknown username returns 401."""
        response = await client.post(
            "/api/auth/login",
            json={
                "username": "nonexistent",
                "password": "SomePass123!",
            },
        )

        assert response.status_code == 401
        assert "Invalid username or password" in response.json()["detail"]

    async def test_login_missing_username(self, client: AsyncClient):
        """Login without username returns 422."""
        response = await client.post(
            "/api/auth/login",
            json={
                "password": "SomePass123!",
            },
        )

        assert response.status_code == 422

    async def test_login_missing_password(self, client: AsyncClient):
        """Login without password returns 422."""
        response = await client.post(
            "/api/auth/login",
            json={
                "username": "someuser",
            },
        )

        assert response.status_code == 422


class TestRefreshToken:
    """Tests for POST /api/auth/refresh endpoint."""

    async def test_refresh_token_success(
        self, client: AsyncClient, test_user_data
    ):
        """Refresh with valid refresh_token returns new access_token."""
        # Register and login
        reg_resp = await client.post("/api/auth/register", json=test_user_data)
        old_refresh = reg_resp.json()["tokens"]["refresh_token"]

        # Refresh
        response = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": old_refresh},
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] > 0

        # New access token should work
        assert data["access_token"] != old_refresh

    async def test_refresh_token_invalid(self, client: AsyncClient):
        """Refresh with invalid token returns 401."""
        response = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": "invalid.token.here"},
        )

        assert response.status_code == 401
        assert "Invalid or expired refresh token" in response.json()["detail"]

    async def test_refresh_token_missing(self, client: AsyncClient):
        """Refresh without refresh_token returns 422."""
        response = await client.post(
            "/api/auth/refresh",
            json={},
        )

        assert response.status_code == 422


class TestLogout:
    """Tests for POST /api/auth/logout endpoint."""

    async def test_logout_success(
        self, client: AsyncClient, auth_headers: dict, test_user_data
    ):
        """Logout with valid token returns 204."""
        # Get refresh token via login (user was already created by auth_headers fixture)
        login_resp = await client.post(
            "/api/auth/login",
            json={
                "username": test_user_data["username"],
                "password": test_user_data["password"],
            },
        )
        assert login_resp.status_code == 200
        refresh_token = login_resp.json()["tokens"]["refresh_token"]

        response = await client.post(
            "/api/auth/logout",
            json={"refresh_token": refresh_token},
            headers=auth_headers,
        )

        assert response.status_code == 204

    async def test_logout_without_auth(self, client: AsyncClient):
        """Logout without authentication returns 403."""
        response = await client.post(
            "/api/auth/logout",
            json={"refresh_token": "some.token"},
        )

        assert response.status_code == 403

    async def test_logout_without_token_body(self, client: AsyncClient, auth_headers):
        """Logout with valid auth but no refresh_token in body returns 204."""
        response = await client.post(
            "/api/auth/logout",
            headers=auth_headers,
        )

        assert response.status_code == 204


class TestAuthFlow:
    """Integration tests for complete auth flows."""

    async def test_register_login_refresh_cycle(
        self, client: AsyncClient, test_user_data
    ):
        """Complete cycle: register → login → refresh."""
        # Register
        reg = await client.post("/api/auth/register", json=test_user_data)
        assert reg.status_code == 201
        user_id = reg.json()["user"]["id"]

        # Login with registered user
        login = await client.post(
            "/api/auth/login",
            json={
                "username": test_user_data["username"],
                "password": test_user_data["password"],
            },
        )
        assert login.status_code == 200
        assert login.json()["user"]["id"] == user_id

        # Refresh token
        refresh = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": login.json()["tokens"]["refresh_token"]},
        )
        assert refresh.status_code == 200
        assert "access_token" in refresh.json()

    async def test_multiple_logins_same_user(
        self, client: AsyncClient, test_user_data
    ):
        """Multiple logins from same user creates new sessions."""
        await client.post("/api/auth/register", json=test_user_data)

        login1 = await client.post(
            "/api/auth/login",
            json={
                "username": test_user_data["username"],
                "password": test_user_data["password"],
            },
        )
        login2 = await client.post(
            "/api/auth/login",
            json={
                "username": test_user_data["username"],
                "password": test_user_data["password"],
            },
        )

        assert login1.status_code == 200
        assert login2.status_code == 200

        # Different tokens
        assert login1.json()["tokens"]["access_token"] != login2.json()["tokens"]["access_token"]
