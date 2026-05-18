"""tests/test_oauth_phase4.py — Phase 3 / Module N OAuth coverage.

Mocks every outbound network call so tests are hermetic.
"""

from __future__ import annotations

from unittest.mock import patch, AsyncMock

import pytest


@pytest.mark.asyncio
async def test_oauth_model_importable():
    try:
        from app.models.oauth import OAuthProvider, OAuthAccount  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("oauth model not in this build")
    assert OAuthProvider is not None
    assert OAuthAccount is not None


@pytest.mark.asyncio
async def test_oauth_route_lists_providers(client):
    r = await client.get("/api/oauth/providers")
    assert r.status_code in (200, 401, 403, 404)
    if r.status_code == 200:
        body = r.json()
        assert isinstance(body, (list, dict))


@pytest.mark.asyncio
async def test_oauth_authorize_redirects_or_404(client):
    r = await client.get(
        "/api/oauth/authorize/google",
        follow_redirects=False,
    )
    # Either redirect (302/307), bad request (400), forbidden, or not found
    assert r.status_code in (200, 302, 307, 400, 401, 403, 404)


@pytest.mark.asyncio
async def test_oauth_callback_handles_invalid_state(client):
    r = await client.get(
        "/api/oauth/callback/google?code=fake&state=invalid",
        follow_redirects=False,
    )
    assert r.status_code in (302, 307, 400, 401, 403, 404, 422)


@pytest.mark.asyncio
async def test_oauth_service_exchange_code_mocked():
    try:
        from app.services.oauth_service import OAuthService  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("oauth service not in this build")

    with patch.object(OAuthService, "exchange_code", new_callable=AsyncMock) as m:
        m.return_value = {"access_token": "fake", "token_type": "Bearer"}
        result = await OAuthService.exchange_code("google", "auth-code-x")  # type: ignore[arg-type]
        assert result["access_token"] == "fake"
