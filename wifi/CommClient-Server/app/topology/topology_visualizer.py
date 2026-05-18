"""Topology visualisation — ASCII + Mermaid renderers.

Two output formats:

  * ``render_ascii(graph)``   — text-mode tree, useful for SSH ops.
  * ``render_mermaid(graph)`` — Mermaid flowchart syntax for
                                Markdown / GitHub / docs.

These are diagnostic helpers; they never mutate the graph.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from app.topology.node_model import Node, NodeType
from app.topology.topology_graph import TopologyGraph


# ── ASCII renderer ──────────────────────────────────────────────


_GLYPH = {
    NodeType.CLIENT:     "👤",
    NodeType.PEER:       "🖥",
    NodeType.ROUTER:     "📡",
    NodeType.BRIDGE:     "🌉",
    NodeType.DISCOVERY:  "🔍",
    NodeType.RELAY:      "↔",
    NodeType.PROXY:      "↻",
    NodeType.FEDERATION: "🔗",
    NodeType.DHT:        "📚",
    NodeType.RENDEZVOUS: "🤝",
}


def _glyph(node: Node) -> str:
    return _GLYPH.get(node.node_type, "•")


def render_ascii(graph: TopologyGraph) -> str:
    """Group nodes by subnet, print one block per subnet, then
    cross-subnet links separately. No fancy box-drawing — keeps the
    output friendly to small terminals + log shipping."""
    nodes = graph.all_nodes()
    if not nodes:
        return "[empty topology]"

    by_subnet: dict[str, list[Node]] = defaultdict(list)
    no_subnet: list[Node] = []
    for n in nodes:
        if n.subnet:
            by_subnet[n.subnet].append(n)
        else:
            no_subnet.append(n)

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("HELEN TOPOLOGY — ASCII view")
    lines.append("=" * 60)
    stats = graph.stats()
    lines.append(
        f"  nodes={stats['node_count']}  links={stats['link_count']}  "
        f"subnets={stats['subnet_count']}  components={stats['components']}"
    )
    lines.append("")

    for subnet, peers in sorted(by_subnet.items()):
        lines.append(f"┌─ Subnet {subnet}")
        for n in sorted(peers, key=lambda x: x.node_id):
            self_marker = "  ← SELF" if n.is_self else ""
            bridge_marker = "  [BRIDGE]" if n.is_bridge() else ""
            lines.append(
                f"│  {_glyph(n)} {n.node_type.value:<10s} "
                f"{n.node_id[:16]:<16s}  "
                f"{n.host}:{n.port}{self_marker}{bridge_marker}"
            )
        lines.append("└" + "─" * 58)
        lines.append("")

    if no_subnet:
        lines.append("Unbound nodes (no subnet inferred):")
        for n in no_subnet:
            lines.append(
                f"  {_glyph(n)} {n.node_type.value} "
                f"{n.node_id[:16]} {n.host}:{n.port}"
            )
        lines.append("")

    # Cross-subnet bridges.
    bridges = graph.bridges()
    if bridges:
        lines.append("Bridges (cross-subnet forwarders):")
        for b in bridges:
            extra = b.extra.get("host_aliases") or []
            extra_str = f"  aliases={','.join(str(a) for a in extra)}" if extra else ""
            lines.append(f"  🌉 {b.node_id[:16]}  {b.host}:{b.port}{extra_str}")
        lines.append("")

    # Top 10 fastest links.
    links = graph.all_links()
    links_with_lat = [L for L in links if L.latency_ms > 0]
    if links_with_lat:
        links_with_lat.sort(key=lambda L: L.latency_ms)
        lines.append("Fastest links (top 10 by latency):")
        for L in links_with_lat[:10]:
            lines.append(
                f"  {L.src_id[:8]} → {L.dst_id[:8]}  "
                f"[{L.link_type.value:<11s}] {L.latency_ms:>6.1f}ms"
            )
        lines.append("")

    components = graph.connected_components()
    if len(components) > 1:
        lines.append(f"⚠ Partition detected: {len(components)} components")
        for i, c in enumerate(components):
            lines.append(f"  Component {i+1}: {len(c)} nodes")

    return "\n".join(lines)


# ── Mermaid renderer ────────────────────────────────────────────


_LINK_STYLE = {
    "lan_direct":  "-->",
    "lan_alias":   "-.->",
    "bridge":      "==>",
    "proxy":       "-.->",
    "relay":       "--->",
    "tunnel":      "===>",
    "hole_punch":  "-..->",
    "federation":  "==>",
    "dht":         "-.->",
}


def render_mermaid(graph: TopologyGraph,
                   include_link_metrics: bool = True) -> str:
    """Flowchart-style Mermaid output. Embed in Markdown ``` mermaid
    blocks for GitHub-flavoured rendering."""
    nodes = graph.all_nodes()
    links = graph.all_links()
    if not nodes:
        return "graph LR\n  empty[empty topology]"

    out: list[str] = ["graph LR"]
    # Subgraph per subnet.
    by_subnet: dict[str, list[Node]] = defaultdict(list)
    no_subnet: list[Node] = []
    for n in nodes:
        if n.subnet:
            by_subnet[n.subnet].append(n)
        else:
            no_subnet.append(n)

    def _id(n: Node) -> str:
        return n.node_id.replace(".", "_").replace(":", "_")[:24]

    for subnet, peers in by_subnet.items():
        safe = subnet.replace(".", "_").replace("/", "_")
        out.append(f"  subgraph subnet_{safe}[{subnet}]")
        for n in peers:
            label = f"{_glyph(n)} {n.node_id[:8]}"
            shape_open, shape_close = (
                ("(((", ")))") if n.is_self else
                ("([", "])") if n.is_bridge() else
                ("[", "]")
            )
            out.append(f"    {_id(n)}{shape_open}{label}{shape_close}")
        out.append("  end")

    for n in no_subnet:
        label = f"{_glyph(n)} {n.node_id[:8]}"
        out.append(f"  {_id(n)}[{label}]")

    # Edges.
    for L in links:
        src_node = graph.node(L.src_id)
        dst_node = graph.node(L.dst_id)
        if not src_node or not dst_node:
            continue
        style = _LINK_STYLE.get(L.link_type.value, "-->")
        if include_link_metrics and L.latency_ms > 0:
            label = f"|{L.latency_ms:.0f}ms|"
        else:
            label = ""
        out.append(f"  {_id(src_node)} {style}{label} {_id(dst_node)}")

    return "\n".join(out)
