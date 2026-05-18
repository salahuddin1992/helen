"""Federation lookup — cross-cluster service discovery.

When ``find_best(...)`` finds nothing in the local cluster, this
module asks the federation gateways for matching records. Gateways
are configured via ``HELEN_FEDERATED_CLUSTERS`` and re-sign with
the destination cluster's secret.

Returned records are *not* persisted into the local registry — they
expire as soon as the caller stops asking. This avoids stale
foreign-cluster state corrupting local lookups.
"""

from __future__ import annotations

import json
from typing import Optional

from app.core.logging import get_logger
from app.service_discovery.discovery_config import get_config
from app.service_discovery.discovery_exceptions import FederationLookupError
from app.service_discovery.service_record import ServiceRecord, ServiceType

logger = get_logger(__name__)


async def lookup_in_cluster(
    cluster_id: str,
    service_type: ServiceType,
    *,
    region: Optional[str] = None,
    k: int = 3,
    timeout: float = 4.0,
) -> list[ServiceRecord]:
    """Ask the federation gateway for top-K records of ``service_type``
    in the named cluster. Returns an empty list on any failure."""
    cfg = get_config()
    if not cfg.enable_federation_lookup:
        return []
    try:
        from app.services.federation_gateway import _parse_cluster_secrets
    except ImportError:
        return []

    secrets = _parse_cluster_secrets()
    secret = secrets.get(cluster_id)
    if not secret:
        # We don't know how to sign for this cluster — give up.
        return []

    try:
        import httpx
        from app.core.federation_auth import sign_request
        from app.services.node_registry import get_registry as get_nr
    except ImportError:
        return []

    # Need a peer in that cluster. Best-effort lookup against the
    # local node_registry.
    reg = get_nr()
    cluster_peer = next(
        (n for n in reg.nodes(include_dead=False)
         if not n.self_node and getattr(n, "extra", {}).get("cluster_id") == cluster_id),
        None,
    )
    # Fallback: any peer (assume same address space).
    if cluster_peer is None:
        cluster_peer = next(
            (n for n in reg.nodes(include_dead=False) if not n.self_node),
            None,
        )
    if cluster_peer is None:
        return []

    body = json.dumps({
        "service_type": service_type.value,
        "region":       region,
        "k":            int(k),
    }).encode()
    path = "/api/discovery/federation/find"
    try:
        headers = sign_request("POST", path, body, secret=secret)
        headers["Content-Type"] = "application/json"
    except Exception:
        return []

    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                f"http://{cluster_peer.host}:{cluster_peer.port}{path}",
                content=body, headers=headers,
            )
        if r.status_code != 200:
            return []
        d = r.json() or {}
        out: list[ServiceRecord] = []
        for raw in d.get("records") or []:
            try:
                out.append(ServiceRecord.from_dict(raw))
            except Exception:
                continue
        return out
    except Exception as e:
        logger.debug("sd_federation_lookup_failed",
                     cluster=cluster_id, error=str(e)[:80])
        return []


async def lookup_across_clusters(
    service_type: ServiceType,
    *,
    k: int = 3,
    region: Optional[str] = None,
) -> list[ServiceRecord]:
    """Fan out to every configured federated cluster, return the
    union (caller will re-score)."""
    try:
        from app.services.federation_gateway import _parse_cluster_secrets
    except ImportError:
        return []
    clusters = list(_parse_cluster_secrets().keys())
    if not clusters:
        return []
    out: list[ServiceRecord] = []
    for cid in clusters:
        records = await lookup_in_cluster(
            cid, service_type, region=region, k=k,
        )
        out.extend(records)
    return out


def serve_federation_request(payload: dict) -> dict:
    """Used by the inbound federation endpoint to answer a lookup."""
    from app.service_discovery.service_lookup import find_top_k
    try:
        st = ServiceType(payload.get("service_type") or ServiceType.PEER.value)
    except ValueError:
        return {"records": []}
    region = payload.get("region")
    k = int(payload.get("k") or 3)
    top = find_top_k(st, k=k, region=region)
    return {
        "records": [r.to_dict() for r, _, _ in top],
    }
