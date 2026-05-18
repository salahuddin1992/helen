"""Node registry — read facade over services.node_registry.

The lower-level ``services.node_registry`` is the source of truth
for hardware + roles + load. This file exposes a stable interface
so callers in the distributed_system package never need to import
the services module directly.
"""

from __future__ import annotations

from typing import Optional

from app.distributed_system.distributed_exceptions import NodeNotFoundError


def list_all(include_dead: bool = False) -> list[dict]:
    try:
        from app.services.node_registry import get_registry
        return get_registry().node_dicts(include_dead=include_dead)
    except Exception:
        return []


def get(node_id: str) -> Optional[dict]:
    for n in list_all(include_dead=True):
        if n.get("node_id") == node_id:
            return n
    return None


def require(node_id: str) -> dict:
    node = get(node_id)
    if node is None:
        raise NodeNotFoundError(f"node {node_id!r} not in registry")
    return node


def fresh_count() -> int:
    return sum(1 for n in list_all(include_dead=False) if n.get("fresh"))


def self_node() -> Optional[dict]:
    for n in list_all(include_dead=True):
        if n.get("self_node"):
            return n
    return None
