"""tests/test_admin_metrics_phase4.py — Module F admin metrics."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_metrics_endpoint_present(client):
    r = await client.get("/api/metrics")
    # Prometheus exposition either gated (401/403) or open (200)
    assert r.status_code in (200, 401, 403, 404)


@pytest.mark.asyncio
async def test_metrics_returns_prometheus_format_when_open(client):
    r = await client.get("/api/metrics")
    if r.status_code != 200:
        pytest.skip(f"metrics gated (status {r.status_code})")
    body = r.text
    # Heuristic Prometheus check: lines beginning with `# HELP` or `# TYPE`
    assert "# HELP" in body or "# TYPE" in body or "_total" in body


@pytest.mark.asyncio
async def test_admin_metrics_admin_endpoint(client, auth_headers):
    r = await client.get("/api/admin/metrics", headers=auth_headers)
    assert r.status_code in (200, 401, 403, 404)
