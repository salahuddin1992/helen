"""Tests for distributed_system.node_registry facade."""

from __future__ import annotations

import pytest

from app.distributed_system import node_registry as nr
from app.distributed_system.distributed_exceptions import NodeNotFoundError


def test_list_all_returns_a_list():
    out = nr.list_all(include_dead=True)
    assert isinstance(out, list)


def test_get_unknown_returns_none():
    assert nr.get("does-not-exist") is None


def test_require_unknown_raises():
    with pytest.raises(NodeNotFoundError):
        nr.require("does-not-exist")


def test_self_node_returned_when_present():
    self_n = nr.self_node()
    if self_n is not None:
        assert self_n.get("self_node") is True


def test_fresh_count_non_negative():
    assert nr.fresh_count() >= 0
