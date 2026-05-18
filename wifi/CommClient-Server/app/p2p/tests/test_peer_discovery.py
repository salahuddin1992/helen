"""Tests for app.p2p.peer_discovery."""

from __future__ import annotations

import pytest

from app.p2p.peer_discovery import discovery_snapshot, sync_from_services


def test_discovery_snapshot_returns_dict():
    s = discovery_snapshot()
    assert isinstance(s, dict)
    assert "p2p_registry" in s


@pytest.mark.asyncio
async def test_sync_from_services_returns_int():
    n = await sync_from_services()
    assert isinstance(n, int)
    assert n >= 0
