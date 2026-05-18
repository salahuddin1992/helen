"""Relay fallback — last-resort routing when no traversal worked.

Hands the request off to the existing peer-relay infrastructure
(``services.cluster_mesh.relay_request``). Always works as long as
*some* path exists through the mesh, even if both ends are behind
strict NATs.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.logging import get_logger
from app.nat.nat_events import emit
from app.nat.nat_exceptions import RelayFallbackError

logger = get_logger(__name__)


async def relay(
    target_peer_id: str,
    method: str,
    path: str,
    body: Any = None,
    headers: Optional[dict] = None,
) -> tuple[int, Any, dict]:
    """Forward a request through the mesh's recursive relay engine.

    Returns the same shape as ``cluster_mesh.relay_request``. Raises
    RelayFallbackError on import / dispatch failure.
    """
    try:
        from app.services.cluster_mesh import relay_request
    except ImportError as e:
        raise RelayFallbackError(f"cluster_mesh missing: {e}")
    try:
        result = await relay_request(
            target_node_id=target_peer_id,
            method=method, path=path, body=body, headers=headers,
        )
    except Exception as e:
        emit("nat.relay_failed", {"target": target_peer_id,
                                    "error": str(e)[:80]})
        raise RelayFallbackError(str(e))
    status, *_ = result
    emit("nat.relay_used", {"target": target_peer_id, "status": status})
    return result


def snapshot() -> dict:
    return {"available": True, "via": "cluster_mesh.relay_request"}
