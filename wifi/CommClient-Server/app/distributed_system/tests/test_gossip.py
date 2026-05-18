"""Tests for distributed_system.gossip_manager."""

from __future__ import annotations

import pytest

from app.distributed_system import gossip_manager as gm


def test_local_state_hash_returns_dict():
    s = gm.local_state_hash()
    assert isinstance(s, dict)


@pytest.mark.asyncio
async def test_trigger_now_runs_without_raising():
    """Even with no peers, triggering one cycle should succeed."""
    out = await gm.trigger_now()
    assert out.get("triggered") is True
