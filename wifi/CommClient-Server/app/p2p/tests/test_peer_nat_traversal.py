"""Tests for app.p2p.peer_nat_traversal."""

from __future__ import annotations

import pytest

from app.p2p.peer_nat_traversal import (
    nat_snapshot, rendezvous_available, supported_strategies, traverse,
)


def test_supported_strategies_contains_relay():
    out = supported_strategies()
    assert "relay" in out


def test_rendezvous_available_returns_bool():
    assert isinstance(rendezvous_available(), bool)


def test_nat_snapshot_keys():
    s = nat_snapshot()
    assert "rendezvous_available" in s
    assert "strategies" in s


@pytest.mark.asyncio
async def test_traverse_returns_relay_at_minimum():
    """Even if hole-punch and tunnel fail, relay is always available."""
    out = await traverse("test-peer-id")
    assert out in supported_strategies()
