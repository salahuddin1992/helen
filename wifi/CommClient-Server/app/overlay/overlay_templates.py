"""Overlay templates — pre-shaped overlay constructors.

Operators rarely build a graph node-by-node from scratch. They want
to say "make me a ring of these 8 peers" or "build a balanced tree
under root". This module provides those builders as pure functions
that return ready-made (nodes, links) collections; the caller hands
them to ``overlay_manager`` to materialise.

Templates supported:

  * ring(name, peer_ids)       — bidirectional ring (each → next).
  * star(name, hub, leaves)    — hub ↔ each leaf.
  * tree(name, root, branching, members) — balanced k-ary tree.
  * full_mesh(name, peer_ids)  — every pair (n × (n-1) links).
  * topic(name, topic, peer_ids) — star with topic tag on every node.
"""

from __future__ import annotations


def _create_overlay_with_nodes(name: str, peer_ids: list[str],
                               *, tags: set[str] | None = None) -> object:
    """Get-or-create the overlay and add every peer as a node."""
    from app.overlay import get_overlay_manager
    mgr = get_overlay_manager()
    g = mgr.create_overlay(name)
    for pid in peer_ids:
        mgr.add_node(name, pid, peer_id=pid, tags=set(tags or set()))
    return g


def build_ring(name: str, peer_ids: list[str], *,
               weight: float = 1.0) -> dict:
    """Each peer ↔ next, last ↔ first. Returns a stats dict."""
    if len(peer_ids) < 2:
        return {"ok": False, "error": "ring_needs_at_least_2"}
    _create_overlay_with_nodes(name, peer_ids, tags={"role:ring"})
    from app.overlay import get_overlay_manager
    mgr = get_overlay_manager()
    n = len(peer_ids)
    for i, src in enumerate(peer_ids):
        dst = peer_ids[(i + 1) % n]
        mgr.add_link(name, src, dst, weight=weight, bidirectional=True)
    return {"ok": True, "name": name, "nodes": n, "links": n}


def build_star(name: str, hub: str, leaves: list[str], *,
               weight: float = 1.0) -> dict:
    if not leaves:
        return {"ok": False, "error": "star_needs_at_least_1_leaf"}
    members = [hub] + list(leaves)
    _create_overlay_with_nodes(name, members, tags={"role:star"})
    from app.overlay import get_overlay_manager
    mgr = get_overlay_manager()
    for leaf in leaves:
        mgr.add_link(name, hub, leaf, weight=weight, bidirectional=True)
    return {"ok": True, "name": name, "nodes": len(members),
            "links": len(leaves)}


def build_tree(name: str, root: str, members: list[str],
               *, branching: int = 2, weight: float = 1.0) -> dict:
    """Balanced k-ary tree. members[0] is the root if it equals root,
    otherwise root is prepended."""
    nodes = list(members)
    if root not in nodes:
        nodes.insert(0, root)
    _create_overlay_with_nodes(name, nodes, tags={"role:tree"})
    from app.overlay import get_overlay_manager
    mgr = get_overlay_manager()
    branching = max(1, int(branching))
    # Index 0 = root; child of node i is i*k+1 ... i*k+k.
    for i, parent in enumerate(nodes):
        for j in range(1, branching + 1):
            child_idx = i * branching + j
            if child_idx >= len(nodes):
                break
            mgr.add_link(name, parent, nodes[child_idx],
                         weight=weight, bidirectional=True)
    return {"ok": True, "name": name, "nodes": len(nodes),
            "branching": branching}


def build_full_mesh(name: str, peer_ids: list[str], *,
                    weight: float = 1.0) -> dict:
    if len(peer_ids) < 2:
        return {"ok": False, "error": "mesh_needs_at_least_2"}
    _create_overlay_with_nodes(name, peer_ids, tags={"role:mesh"})
    from app.overlay import get_overlay_manager
    mgr = get_overlay_manager()
    n = len(peer_ids)
    links = 0
    for i, a in enumerate(peer_ids):
        for b in peer_ids[i + 1:]:
            mgr.add_link(name, a, b, weight=weight, bidirectional=True)
            links += 1
    return {"ok": True, "name": name, "nodes": n, "links": links}


def build_topic(name: str, topic: str, peer_ids: list[str], *,
                hub: str | None = None) -> dict:
    """Star whose hub broadcasts a topic — every node gets a
    ``topic:{topic}`` tag."""
    if not peer_ids:
        return {"ok": False, "error": "topic_needs_at_least_1_peer"}
    chosen_hub = hub or peer_ids[0]
    leaves = [p for p in peer_ids if p != chosen_hub]
    members = [chosen_hub] + leaves
    _create_overlay_with_nodes(
        name, members,
        tags={f"topic:{topic}", "role:topic"},
    )
    from app.overlay import get_overlay_manager
    mgr = get_overlay_manager()
    for leaf in leaves:
        mgr.add_link(name, chosen_hub, leaf, bidirectional=True)
    return {"ok": True, "name": name, "topic": topic,
            "hub": chosen_hub, "leaves": len(leaves)}
