"""Tests for distributed_system.partition_detector facade."""

from __future__ import annotations

import pytest

from app.distributed_system import partition_detector as pd
from app.distributed_system.distributed_exceptions import (
    PartitionDetectedError,
)


def test_is_majority_returns_bool():
    assert isinstance(pd.is_majority(), bool)


def test_is_read_only_returns_bool():
    assert isinstance(pd.is_read_only(), bool)


def test_snapshot_returns_dict():
    s = pd.snapshot()
    assert isinstance(s, dict)


def test_require_majority_does_not_raise_on_majority(monkeypatch):
    """When we're majority, the helper is a no-op."""
    monkeypatch.setattr(pd, "is_majority", lambda: True)
    pd.require_majority()


def test_require_majority_raises_on_minority(monkeypatch):
    monkeypatch.setattr(pd, "is_majority", lambda: False)
    with pytest.raises(PartitionDetectedError):
        pd.require_majority()
