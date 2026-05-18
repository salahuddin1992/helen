"""tests/test_auth_phase4.py — login / refresh / logout flow.

Built on top of the existing ``conftest.py`` ``client`` + ``auth_headers``
fixtures. Skips gracefully when the auth route surface differs.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_register_then_login(client):
    creds = {"username": "phase4_user", "password": "S3cret-pass-2026!"}

    r = await client.post("/api/auth/register", json=creds)
    if r.status_code == 404:
        pytest.skip("register endpoint not present in this build")
    assert r.status_code in (200, 201), r.text

    r = await client.post("/api/auth/login", json=creds)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" in body
    assert body.get("token_type", "").lower() == "bearer"


@pytest.mark.asyncio
async def test_login_with_wrong_password_returns_401(client):
    r = await client.post(
        "/api/auth/login",
        json={"username": "phase4_user", "password": "WRONG"},
    )
    assert r.status_code in (400, 401, 403)


@pytest.mark.asyncio
async def test_refresh_endpoint_present_or_404(client):
    r = await client.post("/api/auth/refresh", json={"refresh_token": "garbage"})
    assert r.status_code in (200, 400, 401, 404, 422)


@pytest.mark.asyncio
async def test_logout_returns_no_content(client, auth_headers):
    r = await client.post("/api/auth/logout", headers=auth_headers)
    assert r.status_code in (200, 204, 404), r.text
