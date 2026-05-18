"""Peer proxy — single-hop HTTP forwarder."""

from __future__ import annotations

from typing import Any, Optional

from app.p2p.peer_connection import request
from app.p2p.peer_model import Peer


async def proxy_through(
    proxy: Peer,
    target_peer_id: str,
    method: str,
    path: str,
    body: Any = None,
    headers: Optional[dict] = None,
) -> tuple[int, Any, dict]:
    """Send request to the target via ``proxy`` (one hop).

    The proxy node receives a POST to /api/cluster/relay, which
    re-enters its local relay engine.
    """
    payload = {
        "target_node_id": target_peer_id,
        "method": method,
        "path": path,
        "body": body,
        "_hops_remaining": 1,
    }
    return await request(
        proxy, method="POST", path="/api/cluster/relay",
        body=payload, headers=headers,
    )
