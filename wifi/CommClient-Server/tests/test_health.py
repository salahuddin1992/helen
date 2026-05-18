"""
Health and system information endpoint tests.

Covers health checks and server info endpoints.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestHealthCheck:
    """Tests for GET /health endpoint."""

    async def test_health_check_success(self, client: AsyncClient):
        """Health check returns 200 with expected fields."""
        response = await client.get("/api/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "service" in data
        assert "version" in data
        assert data["service"] == "Helen Server"

    async def test_health_check_has_version(self, client: AsyncClient):
        """Health endpoint returns version field."""
        response = await client.get("/api/health")

        data = response.json()
        assert "version" in data
        assert isinstance(data["version"], str)
        assert len(data["version"]) > 0

    async def test_health_check_no_auth_required(self, client: AsyncClient):
        """Health check can be accessed without authentication."""
        response = await client.get("/api/health")
        assert response.status_code == 200


class TestServerInfo:
    """Tests for GET /info endpoint."""

    async def test_server_info_success(self, client: AsyncClient):
        """Server info returns 200 with expected fields."""
        response = await client.get("/api/info")

        assert response.status_code == 200
        data = response.json()
        assert "service" in data
        assert "version" in data
        assert "lan_ip" in data
        assert "uptime_seconds" in data
        assert "online_users" in data

    async def test_server_info_has_valid_values(self, client: AsyncClient):
        """Server info returns valid values for all fields."""
        response = await client.get("/api/info")

        data = response.json()
        assert data["service"] == "Helen Server"
        assert isinstance(data["version"], str)
        assert isinstance(data["uptime_seconds"], int)
        assert data["uptime_seconds"] >= 0
        assert isinstance(data["online_users"], int)
        assert data["online_users"] >= 0

    async def test_server_info_has_lan_ip(self, client: AsyncClient):
        """Server info includes LAN IP address."""
        response = await client.get("/api/info")

        data = response.json()
        assert "lan_ip" in data
        lan_ip = data["lan_ip"]
        # LAN IP should be a valid IP-like string (may be None in test env)
        if lan_ip is not None:
            assert isinstance(lan_ip, str)

    async def test_server_info_no_auth_required(self, client: AsyncClient):
        """Server info can be accessed without authentication."""
        response = await client.get("/api/info")
        assert response.status_code == 200

    async def test_info_endpoint_format(self, client: AsyncClient):
        """Info endpoint returns JSON in expected format."""
        response = await client.get("/api/info")

        data = response.json()
        # Should be a dict with specific keys
        assert isinstance(data, dict)
        assert set(data.keys()) >= {
            "service",
            "version",
            "uptime_seconds",
            "online_users",
        }
