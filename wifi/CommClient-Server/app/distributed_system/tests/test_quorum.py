"""Tests for distributed_system.quorum_manager."""

from __future__ import annotations

import pytest

from app.distributed_system.quorum_manager import required_acks, write, read


def test_required_acks_strict_majority():
    assert required_acks(1) == 1
    assert required_acks(2) == 2  # ⌊2/2⌋ + 1 = 2
    assert required_acks(3) == 2
    assert required_acks(5) == 3
    assert required_acks(7) == 4


def test_required_acks_override():
    assert required_acks(7, override=2) == 2
    assert required_acks(7, override=0) == 1  # min 1


@pytest.mark.asyncio
async def test_write_returns_expected_keys():
    out = await write("ds_quorum_test", "k1", {"x": 1},
                       replication=1, timeout=2.0)
    expected = {"accepted", "acks_received", "acks_required",
                "duration_ms", "failures"}
    assert expected.issubset(out.keys())


@pytest.mark.asyncio
async def test_read_unknown_key_returns_none_or_dict():
    val = await read("ds_quorum_test", "no_such_xx",
                      replication=1, timeout=2.0)
    assert val is None or isinstance(val, dict)
