"""Bootstrap peer support — well-known seeds for first-contact.

A bootstrap peer is one whose host:port is provided out-of-band
(env var ``HELEN_BOOTSTRAP_PEERS=host:port,host2:port,...``). On
startup the p2p manager pings each one to seed the registry, then
relies on gossip + UDP discovery for the rest.
"""

from __future__ import annotations

import os
import time

from app.p2p.peer_events import emit
from app.p2p.peer_model import Peer, PeerRole
from app.p2p.peer_registry import get_p2p_registry


def parse_bootstrap_env() -> list[tuple[str, int]]:
    raw = os.environ.get("HELEN_BOOTSTRAP_PEERS") or ""
    out: list[tuple[str, int]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            host, port_s = entry.rsplit(":", 1)
            try:
                out.append((host, int(port_s)))
            except ValueError:
                continue
        else:
            out.append((entry, 3000))
    return out


async def seed_bootstrap_peers() -> int:
    """For each configured bootstrap peer, hit ``/api/cluster/info``,
    insert into the registry, return count of successful seeds."""
    pairs = parse_bootstrap_env()
    if not pairs:
        return 0
    try:
        import httpx
    except ImportError:
        return 0
    reg = get_p2p_registry()
    n = 0
    async with httpx.AsyncClient(timeout=3.0) as client:
        for host, port in pairs:
            try:
                r = await client.get(f"http://{host}:{port}/api/cluster/info")
                if r.status_code != 200:
                    continue
                d = r.json() or {}
                pid = str(d.get("node_id") or d.get("server_id") or "")
                if not pid:
                    continue
                p = Peer(
                    peer_id=pid,
                    role=PeerRole.BOOTSTRAP,
                    host=host, port=port,
                    cluster_id=str(d.get("cluster_id") or "default"),
                    capabilities=dict(d.get("capability") or {}),
                    last_seen=time.time(),
                    extra={"source": "bootstrap"},
                )
                reg.upsert(p)
                n += 1
                emit("bootstrap.seeded", {"peer_id": pid,
                                           "host": host, "port": port})
            except Exception:
                continue
    return n
