"""Federation gateway — cross-cluster bridge with key swap.

A node configured with ``HELEN_FEDERATED_CLUSTERS=cluster_b:secret_b,...``
can talk to peers in *other* clusters by re-signing requests with
the destination's HMAC secret instead of the local one.

Flow:

    local_caller → gateway.forward(target_cluster, peer_id, request)
        ↓
    1. lookup secret for target_cluster from FEDERATED_CLUSTERS map
    2. resolve peer endpoint via gossip / DHT (best effort)
    3. re-sign request with target_cluster's secret
    4. POST to /api/cluster/relay on the target peer
    5. return response

This is *additive*: the existing same-cluster federation flow keeps
working through cluster_id-derived auto-secrets. The gateway only
kicks in when the destination cluster differs from ours.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


def _parse_cluster_secrets() -> dict[str, str]:
    """Parse HELEN_FEDERATED_CLUSTERS env into {cluster_id: secret}.

    Format: ``cluster_a:secret_a,cluster_b:secret_b``. Empty values are
    skipped. Secrets shorter than 8 chars are rejected with a warning.
    """
    raw = os.environ.get("HELEN_FEDERATED_CLUSTERS", "") or ""
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        cid, secret = part.split(":", 1)
        cid, secret = cid.strip(), secret.strip()
        if not cid or len(secret) < 8:
            logger.warning("federation_gateway_short_secret", cluster_id=cid)
            continue
        out[cid] = secret
    return out


def known_clusters() -> list[str]:
    return sorted(_parse_cluster_secrets().keys())


def _secret_for(cluster_id: str) -> Optional[str]:
    return _parse_cluster_secrets().get(cluster_id)


def _derive_default_secret(cluster_id: str) -> bytes:
    """Same derivation as federation_auth._effective_secret so a peer
    that didn't pin a secret still resolves to a deterministic key."""
    return hashlib.sha256(
        f"helen-lan-cluster:{cluster_id}".encode()
    ).digest()


async def forward(
    target_cluster: str,
    target_peer_id: str,
    method: str,
    path: str,
    body: Any = None,
    headers: Optional[dict] = None,
    *,
    timeout: float = 5.0,
) -> tuple[int, Any, dict]:
    """Re-sign + forward a request to ``(target_cluster, target_peer_id)``.

    Returns the same shape as ``cluster_mesh.relay_request``. Raises
    ``RuntimeError`` if the cluster has no known endpoint.
    """
    try:
        import httpx
        from app.core.federation_auth import (
            HEADER_TIMESTAMP, HEADER_SIGNATURE, HEADER_ORIGIN, sign_request,
        )
        from app.services.discovery_service import get_server_id
    except ImportError as e:
        raise RuntimeError(f"federation primitives missing: {e}")

    secret = _secret_for(target_cluster)
    secret_bytes = (secret.encode() if secret
                    else _derive_default_secret(target_cluster))

    body_bytes = (
        json.dumps(body).encode() if body is not None else b""
    )
    sig_headers = sign_request(
        method=method, path=path, body=body_bytes,
        secret=secret_bytes.decode(errors="replace") if secret else None,
    )
    out_headers = {**(headers or {}), **sig_headers}
    out_headers[HEADER_ORIGIN] = get_server_id() or ""

    # Resolve the peer endpoint. We rely on the local node_registry
    # learning foreign-cluster peers via gossip — for fully decoupled
    # clusters with no shared gossip, the operator should pre-seed
    # via HELEN_BOOTSTRAP_PEERS.
    try:
        from app.services.node_registry import get_registry
        node = next(
            (n for n in get_registry().nodes(include_dead=True)
             if n.node_id == target_peer_id),
            None,
        )
    except Exception:
        node = None
    if node is None or not node.host:
        return 404, {"error": "unknown_peer", "cluster": target_cluster,
                      "peer": target_peer_id}, {}

    url = f"http://{node.host}:{node.port}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, url, content=body_bytes,
                                headers=out_headers)
        try:
            return r.status_code, r.json(), dict(r.headers)
        except Exception:
            return r.status_code, r.text, dict(r.headers)
    except Exception as e:
        return 502, {"error": "transport", "detail": str(e)[:80]}, {}


def status() -> dict:
    return {
        "configured_clusters": known_clusters(),
        "count":               len(known_clusters()),
        "env_var":             "HELEN_FEDERATED_CLUSTERS",
    }
