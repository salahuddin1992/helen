"""Tests for app.p2p.peer_dht."""

from __future__ import annotations

import pytest

from app.p2p.peer_dht import dht_snapshot, lookup, store_local


def test_dht_snapshot_returns_dict():
    assert isinstance(dht_snapshot(), dict)


def test_store_local_returns_bool():
    """Storing should not raise; True or False either is OK."""
    result = store_local("user-test-xx", "server-test-xx")
    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_lookup_returns_dict_or_none():
    out = await lookup("user-never-stored-xx")
    assert out is None or isinstance(out, dict)
