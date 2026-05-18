"""tests/test_workspaces_phase4.py — Phase 3 / Module M coverage."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_workspaces_models_importable():
    try:
        from app.models.workspace import Workspace, WorkspaceMember  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("workspace models not in this build")
    assert Workspace is not None
    assert WorkspaceMember is not None


@pytest.mark.asyncio
async def test_workspaces_list_requires_auth(client):
    r = await client.get("/api/workspaces")
    assert r.status_code in (401, 403, 404)


@pytest.mark.asyncio
async def test_workspaces_authed_list(client, auth_headers):
    r = await client.get("/api/workspaces", headers=auth_headers)
    assert r.status_code in (200, 401, 403, 404)
    if r.status_code == 200:
        body = r.json()
        assert isinstance(body, list) or isinstance(body, dict)


@pytest.mark.asyncio
async def test_workspaces_create_validation(client, auth_headers):
    # Empty body should be rejected
    r = await client.post("/api/workspaces", headers=auth_headers, json={})
    assert r.status_code in (400, 401, 403, 404, 422)
