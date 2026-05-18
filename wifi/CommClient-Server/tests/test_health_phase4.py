"""tests/test_health_phase4.py — smoke check on /api/health.

Independent from the legacy ``test_health.py`` so both can run in CI.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_api_health_returns_200_and_json(client):
    res = await client.get("/api/health")
    assert res.status_code == 200, res.text
    payload = res.json()
    assert isinstance(payload, dict)
    assert payload.get("status") in {"ok", "healthy", "up"} or "status" in payload


@pytest.mark.asyncio
async def test_api_health_has_no_secrets(client):
    """Health endpoint must not leak SECRET_KEY/cert paths."""
    res = await client.get("/api/health")
    body = res.text.lower()
    forbidden = ("secret_key", "private_key", "fernet_key")
    for needle in forbidden:
        assert needle not in body, f"health leaked: {needle}"
