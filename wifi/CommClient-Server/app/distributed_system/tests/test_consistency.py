"""Tests for distributed_system.consistency_manager."""

from __future__ import annotations

import pytest

from app.distributed_system.consistency_manager import (
    ConsistencyLevel, write, read,
)
from app.distributed_system.distributed_exceptions import ConsistencyError


@pytest.mark.asyncio
async def test_eventual_write_returns_record():
    rec = await write("ds_test", "consist_eventual_key",
                       {"a": 1}, level=ConsistencyLevel.EVENTUAL)
    assert rec["version"] >= 1


@pytest.mark.asyncio
async def test_read_local_returns_what_we_wrote():
    await write("ds_test", "consist_read_key",
                 {"v": 42}, level=ConsistencyLevel.EVENTUAL)
    got = await read("ds_test", "consist_read_key",
                      level=ConsistencyLevel.READ_LOCAL)
    assert got is not None
    assert got["value"] == {"v": 42}


@pytest.mark.asyncio
async def test_invalid_write_level_raises():
    with pytest.raises(ConsistencyError):
        await write("ds_test", "x", {}, level=ConsistencyLevel.READ_LOCAL)


@pytest.mark.asyncio
async def test_invalid_read_level_does_not_raise_for_write_levels():
    # WRITE_QUORUM as a *read* level just falls back to local read.
    val = await read("ds_test", "no_such_key_xx",
                      level=ConsistencyLevel.WRITE_QUORUM)
    assert val is None
