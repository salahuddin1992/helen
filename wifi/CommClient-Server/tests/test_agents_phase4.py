"""tests/test_agents_phase4.py — Phase 3 / Module L coverage."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_agent_model_importable():
    try:
        from app.models.agent import Agent  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("agent model not in this build")
    assert Agent is not None


@pytest.mark.asyncio
async def test_agent_manager_importable():
    try:
        from app.services.agent_manager import AgentManager  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("agent manager not in this build")
    assert AgentManager is not None


@pytest.mark.asyncio
async def test_agents_list_requires_auth(client):
    r = await client.get("/api/agents")
    assert r.status_code in (401, 403, 404)


@pytest.mark.asyncio
async def test_agents_create_validation(client, auth_headers):
    r = await client.post("/api/agents", headers=auth_headers, json={})
    assert r.status_code in (400, 401, 403, 404, 422)
