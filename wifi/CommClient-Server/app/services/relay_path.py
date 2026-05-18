"""
Cross-server relay path finder and chain orchestrator.

Given a source server (us) and a destination server that is reachable
only through intermediate Helen servers (bridges/regions), this module:

  1. Crawls the federation graph by calling `/federation/peers` on each
     known peer, breadth-first, until we find the destination or run
     out of servers within `MAX_HOPS`.
  2. Returns the shortest path as a list of `PeerRecord`s.
  3. Builds the relay chain end-to-end by calling each hop's
     `/federation/relay/alloc` in reverse order — the last hop is
     programmed with the final destination as its `next_hop`, the
     second-to-last hop points at the last hop's ingress port, and so
     on, until the first hop's `(ingress_host, ingress_port)` is what
     the client dials.

Topology snapshots are cached for `_TOPOLOGY_CACHE_TTL` seconds so we
don't re-crawl the mesh for every call attempt. A failed alloc along
the chain rolls back every relay we already opened.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.services.discovery_service import get_server_id
from app.services.federation_service import federation_service
from app.services.peer_registry import PeerRecord, peer_registry

logger = get_logger(__name__)

MAX_HOPS = 6
_TOPOLOGY_CACHE_TTL = 30.0  # seconds


@dataclass
class RelayHop:
    """One allocated link in a chain."""
    server_id: str
    relay_id: str
    ingress_host: str
    ingress_port: int
    peer: PeerRecord  # peer that owns this hop


@dataclass
class RelayChain:
    """End-to-end chain. Client dials (entry_host, entry_port)."""
    entry_host: str
    entry_port: int
    hops: list[RelayHop] = field(default_factory=list)
    final_next_hop: tuple[str, int] | None = None  # (host, port)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_host": self.entry_host,
            "entry_port": self.entry_port,
            "final_next_hop": (
                {"host": self.final_next_hop[0], "port": self.final_next_hop[1]}
                if self.final_next_hop else None
            ),
            "hops": [
                {
                    "server_id": h.server_id,
                    "relay_id": h.relay_id,
                    "ingress_host": h.ingress_host,
                    "ingress_port": h.ingress_port,
                }
                for h in self.hops
            ],
        }


# ── Topology crawl ─────────────────────────────────────────

# server_id → (list of neighbor PeerRecord-ish dicts, expires_at)
_topology_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}


async def _fetch_peers(peer: PeerRecord) -> list[dict[str, Any]]:
    """Ask one peer for *its* neighbors. Returns [] on failure."""
    resp = await federation_service._signed_request(  # intentional: reuse plumbing
        peer, "GET", "/api/federation/peers",
    )
    if resp is None or resp.status_code != 200:
        return []
    try:
        body = resp.json()
    except ValueError:
        return []
    return list(body.get("peers") or [])


async def discover_topology(max_depth: int = MAX_HOPS) -> dict[str, list[dict[str, Any]]]:
    """BFS the federation graph starting from our direct peers.

    Returns a dict `{server_id: [neighbor_dict, ...]}`. The `server_id`
    key is included for every server we could reach; neighbor_dicts have
    shape `{server_id, name, host, port, protocol}`.

    Own-server entry is NOT included — paths start from our direct peers.
    """
    # Seed with our own direct peers.
    direct = await peer_registry.list(include_stale=False)
    if not direct:
        return {}

    graph: dict[str, list[dict[str, Any]]] = {}
    peer_records: dict[str, PeerRecord] = {p.server_id: p for p in direct}

    # Us → our direct peers
    us = get_server_id()
    graph[us] = [
        {"server_id": p.server_id, "host": p.host, "port": p.port,
         "protocol": p.protocol, "name": p.name}
        for p in direct
    ]

    # BFS
    frontier: list[PeerRecord] = list(direct)
    seen: set[str] = {us, *(p.server_id for p in direct)}
    depth = 1

    while frontier and depth < max_depth:
        next_frontier: list[PeerRecord] = []
        # Ask all frontier nodes in parallel.
        results = await asyncio.gather(
            *[_get_cached_peers(p) for p in frontier], return_exceptions=True,
        )
        for p, neighbors in zip(frontier, results):
            if isinstance(neighbors, Exception):
                graph[p.server_id] = []
                continue
            graph[p.server_id] = neighbors
            for n in neighbors:
                sid = n.get("server_id")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                # Synthesize a PeerRecord-enough object to call later.
                if sid not in peer_records:
                    peer_records[sid] = PeerRecord(
                        server_id=sid,
                        name=str(n.get("name", "?")),
                        host=str(n.get("host", "")),
                        port=int(n.get("port") or 0),
                        version="?",
                        protocol=str(n.get("protocol", "http")),
                        users_online=0,
                        uptime=0,
                        first_seen=time.time(),
                        last_seen=time.time(),
                        from_ip=str(n.get("host", "")),
                    )
                next_frontier.append(peer_records[sid])
        frontier = next_frontier
        depth += 1

    # Stash peer records on the module so build_chain can find them later.
    _known_peers.update(peer_records)
    return graph


async def _get_cached_peers(peer: PeerRecord) -> list[dict[str, Any]]:
    entry = _topology_cache.get(peer.server_id)
    now = time.time()
    if entry is not None and entry[1] > now:
        return entry[0]
    neighbors = await _fetch_peers(peer)
    _topology_cache[peer.server_id] = (neighbors, now + _TOPOLOGY_CACHE_TTL)
    return neighbors


# Peers we've learned about transitively (not directly LAN-visible).
_known_peers: dict[str, PeerRecord] = {}


def _resolve_peer(server_id: str) -> PeerRecord | None:
    return _known_peers.get(server_id)


# ── Shortest-path BFS ──────────────────────────────────────


def _shortest_path(
    graph: dict[str, list[dict[str, Any]]],
    src: str,
    dst: str,
) -> list[str] | None:
    """BFS over server_ids. Returns the path *excluding* src, *including* dst."""
    if src == dst:
        return []
    parents: dict[str, str] = {src: ""}
    queue: list[str] = [src]
    while queue:
        node = queue.pop(0)
        for n in graph.get(node, []):
            nid = n.get("server_id")
            if not nid or nid in parents:
                continue
            parents[nid] = node
            if nid == dst:
                # Reconstruct
                out: list[str] = []
                cur = dst
                while cur and cur != src:
                    out.append(cur)
                    cur = parents[cur]
                out.reverse()
                return out
            queue.append(nid)
    return None


# ── Chain builder ──────────────────────────────────────────


async def _alloc_on_peer(
    peer: PeerRecord,
    next_hop_host: str,
    next_hop_port: int,
    idle_ttl: float,
) -> dict[str, Any] | None:
    """Call `/federation/relay/alloc` on `peer`."""
    resp = await federation_service._signed_request(
        peer, "POST", "/api/federation/relay/alloc",
        json_body={
            "next_hop_host": next_hop_host,
            "next_hop_port": next_hop_port,
            "idle_ttl_seconds": idle_ttl,
        },
    )
    if resp is None or resp.status_code not in (200, 201):
        return None
    try:
        return resp.json()
    except ValueError:
        return None


async def _release_on_peer(peer: PeerRecord, relay_id: str) -> None:
    try:
        await federation_service._signed_request(
            peer, "POST", "/api/federation/relay/release",
            json_body={"relay_id": relay_id},
        )
    except Exception as e:
        logger.debug("relay_release_error", peer=peer.server_id,
                     relay_id=relay_id, error=str(e))


async def build_chain(
    dst_server_id: str,
    dst_host: str,
    dst_port: int,
    idle_ttl: float = 180.0,
) -> RelayChain | None:
    """Open a relay chain from us to `dst_host:dst_port` on `dst_server_id`.

    `dst_host:dst_port` is the final destination — typically a client's
    ICE candidate on the destination server's LAN. The chain is allocated
    in reverse (destination side first) so each upstream hop can be
    programmed with a valid `next_hop` from the start.

    Returns `None` if no path exists; on any mid-chain failure every
    already-opened relay is torn down before returning.
    """
    me = get_server_id()
    if dst_server_id == me:
        # No federation needed — caller should talk to dst directly.
        return RelayChain(entry_host=dst_host, entry_port=dst_port,
                          final_next_hop=(dst_host, dst_port))

    graph = await discover_topology()
    if dst_server_id not in {
        n.get("server_id") for neighbors in graph.values() for n in neighbors
    }:
        logger.warning("relay_path_no_topology_entry", dst=dst_server_id)
        return None

    path_sids = _shortest_path(graph, me, dst_server_id)
    if path_sids is None:
        logger.warning("relay_path_not_found", dst=dst_server_id)
        return None

    # path_sids excludes us, includes dst. The servers that must host a
    # relay are path_sids *without* the final dst — i.e. every transit
    # server between us and dst. If dst is a direct neighbor, len(path) ==
    # 1 and we allocate one relay on dst, pointing at (dst_host, dst_port).
    # Actually — dst is the server that already hosts the client; we do
    # not need to allocate a relay there. The transit set is path[:-1].
    # But for a single-hop (direct neighbor), that leaves zero hops and
    # the client would talk directly to dst_host:dst_port, which is what
    # the existing federation ICE path already does. We still want an
    # entry point on dst for cases where dst_host isn't reachable from us
    # — so we always allocate at least one relay on dst (the last hop).
    transit_sids = path_sids  # include dst as the last relay

    # Resolve each transit server_id to a PeerRecord.
    transit_peers: list[PeerRecord] = []
    for sid in transit_sids:
        p = _resolve_peer(sid) or await peer_registry.get(sid)
        if p is None:
            logger.warning("relay_path_peer_unresolvable", server_id=sid)
            return None
        transit_peers.append(p)

    # Allocate in reverse: last peer first, programmed with final dst.
    hops: list[RelayHop] = []
    current_next_hop = (dst_host, dst_port)
    try:
        for peer in reversed(transit_peers):
            alloc = await _alloc_on_peer(
                peer, current_next_hop[0], current_next_hop[1], idle_ttl,
            )
            if alloc is None:
                raise RuntimeError(
                    f"alloc failed on {peer.server_id} ({peer.host}:{peer.port})"
                )
            hop = RelayHop(
                server_id=peer.server_id,
                relay_id=alloc["relay_id"],
                ingress_host=alloc["ingress_host"],
                ingress_port=int(alloc["ingress_port"]),
                peer=peer,
            )
            hops.append(hop)
            current_next_hop = (hop.ingress_host, hop.ingress_port)
    except Exception as e:
        logger.warning("relay_chain_alloc_failed", error=str(e))
        try:
            from app.services.federation_metrics import incr
            incr("relay_chains_failed")
        except Exception:
            pass
        # Roll back in parallel.
        await asyncio.gather(
            *[_release_on_peer(h.peer, h.relay_id) for h in hops],
            return_exceptions=True,
        )
        return None

    # `hops` is in reverse order (farthest first). Reverse to match
    # client-dialed direction so consumers can iterate head→tail.
    hops.reverse()
    chain = RelayChain(
        entry_host=hops[0].ingress_host,
        entry_port=hops[0].ingress_port,
        hops=hops,
        final_next_hop=(dst_host, dst_port),
    )
    logger.info(
        "relay_chain_built",
        dst=dst_server_id,
        hop_count=len(hops),
        entry=f"{chain.entry_host}:{chain.entry_port}",
    )
    try:
        from app.services.federation_metrics import incr
        incr("relay_chains_built")
    except Exception:
        pass
    return chain


async def teardown_chain(chain: RelayChain) -> None:
    """Best-effort release every hop."""
    await asyncio.gather(
        *[_release_on_peer(h.peer, h.relay_id) for h in chain.hops],
        return_exceptions=True,
    )
    logger.info("relay_chain_released", hop_count=len(chain.hops))
