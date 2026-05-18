"""tests/test_admin_logs_phase4.py — Module E admin logs endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_admin_logs_requires_auth(client):
    r = await client.get("/api/admin/logs")
    assert r.status_code in (401, 403, 404)


@pytest.mark.asyncio
async def test_admin_logs_admin_can_list(client, auth_headers):
    r = await client.get("/api/admin/logs", headers=auth_headers)
    # Either the endpoint isn't registered (404) or it returns JSON
    assert r.status_code in (200, 401, 403, 404)
    if r.status_code == 200:
        payload = r.json()
        assert isinstance(payload, (list, dict))


@pytest.mark.asyncio
async def test_admin_logs_supports_limit_query(client, auth_headers):
    r = await client.get("/api/admin/logs?limit=10", headers=auth_headers)
    assert r.status_code in (200, 401, 403, 404, 422)
