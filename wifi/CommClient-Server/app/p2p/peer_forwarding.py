"""Peer forwarding — multi-hop message hand-off via existing relay.

Wraps services.cluster_mesh.relay_request so the p2p layer has a
single stable forwarding entry point.
"""

from __future__ import annotations

from typing import Any, Optional

from app.p2p.p2p_config import get_config
from app.p2p.p2p_exceptions import PeerForwardingError


async def forward(
    target_peer_id: str,
    method: str,
    path: str,
    body: Any = None,
    headers: Optional[dict] = None,
    *,
    hops_remaining: Optional[int] = None,
) -> tuple[int, Any, dict]:
    cfg = get_config()
    try:
        from app.services.cluster_mesh import relay_request
    except ImportError as e:
        raise PeerForwardingError(f"cluster_mesh missing: {e}")
    return await relay_request(
        target_node_id=target_peer_id,
        method=method, path=path, body=body, headers=headers,
        hops_remaining=hops_remaining if hops_remaining is not None
                       else cfg.max_forward_hops,
    )
