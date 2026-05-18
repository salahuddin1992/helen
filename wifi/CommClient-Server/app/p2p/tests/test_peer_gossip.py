"""Tests for app.p2p.peer_gossip."""

from __future__ import annotations

import pytest

from app.p2p.peer_gossip import (
    gossip_snapshot, known_state_hashes_count, trigger_gossip_cycle,
)


def test_known_state_hashes_count_returns_int():
    assert isinstance(known_state_hashes_count(), int)


def test_gossip_snapshot_returns_dict():
    s = gossip_snapshot()
    assert "known_state_hashes_count" in s


@pytest.mark.asyncio
async def test_trigger_gossip_returns_dict():
    out = await trigger_gossip_cycle()
    assert isinstance(out, dict)
    assert "triggered" in out
