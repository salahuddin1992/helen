"""Tests for app.p2p.peer_failover."""

from __future__ import annotations

import pytest

from app.p2p.peer_failover import try_with_failover
from app.p2p.peer_model import Peer, PeerRole


def _peer(pid: str) -> Peer:
    return Peer(peer_id=pid, role=PeerRole.RELAY, host="1.1.1.1", port=3000)


@pytest.mark.asyncio
async def test_first_success_short_circuits():
    calls = []

    async def attempt(p):
        calls.append(p.peer_id)
        return True

    cands = [_peer("a"), _peer("b"), _peer("c")]
    ok, used, attempted = await try_with_failover(
        attempt, candidates=cands, max_attempts=3,
    )
    assert ok is True
    assert used.peer_id == "a"
    assert attempted == ["a"]


@pytest.mark.asyncio
async def test_retries_on_failure():
    calls = []

    async def attempt(p):
        calls.append(p.peer_id)
        return p.peer_id == "c"  # only c succeeds

    cands = [_peer("a"), _peer("b"), _peer("c")]
    ok, used, attempted = await try_with_failover(
        attempt, candidates=cands, max_attempts=3,
    )
    assert ok is True
    assert used.peer_id == "c"
    assert attempted == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_exhausted_returns_false():
    async def attempt(p):
        return False

    cands = [_peer("a"), _peer("b")]
    ok, used, attempted = await try_with_failover(
        attempt, candidates=cands, max_attempts=2,
    )
    assert ok is False
    assert used is None
    assert attempted == ["a", "b"]


@pytest.mark.asyncio
async def test_exception_treated_as_failure():
    async def attempt(p):
        raise RuntimeError("boom")

    cands = [_peer("a")]
    ok, used, attempted = await try_with_failover(
        attempt, candidates=cands, max_attempts=1,
    )
    assert ok is False
