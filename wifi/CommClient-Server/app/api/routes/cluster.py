"""
Cluster mesh public endpoints.

All routes here are mounted at /api/cluster/* and do NOT require auth —
peer Helen servers on the LAN hit them to discover capability + load
and to request traffic relay. In hostile networks, firewall the port.

Endpoints:
  GET  /api/cluster/info            — self capability + load + known peers
  POST /api/cluster/relay           — proxy an HTTP request to another node
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

router = APIRouter(tags=["cluster-mesh"])


@router.get("/cluster/info")
async def cluster_info():
    """Self-identity payload used by peers to auto-register us.

    Includes a trimmed view of our known_peers list so the caller
    can absorb them transitively — mesh convergence without
    central coordination.
    """
    from app.services.node_registry import get_registry
    reg = get_registry()
    reg.refresh_self_load()
    me = next((n for n in reg.nodes() if n.self_node), None)
    if not me:
        raise HTTPException(status_code=503, detail="registry not ready")
    # Known peers — only the fresh ones, to keep payload small.
    known = []
    for n in reg.nodes(include_dead=False):
        if n.self_node:
            continue
        known.append({
            "node_id": n.node_id,
            "host":    n.host,
            "port":    n.port,
        })
    # Truncate to 50 known peers per gossip/probe payload.
    known = known[:50]
    return {
        "node_id":    me.node_id,
        "host":       me.host,
        "port":       me.port,
        "capability": {
            "cpu_cores": me.capability.cpu_cores,
            "ram_gb":    me.capability.ram_gb,
            "nic_gbps":  me.capability.nic_gbps,
            "disk_ssd":  me.capability.disk_ssd,
            "platform":  me.capability.platform,
            "version":   me.capability.version,
        },
        "roles": {
            "signaling":     me.roles.signaling,
            "messaging":     me.roles.messaging,
            "presence":      me.roles.presence,
            "sfu":           me.roles.sfu,
            "relay":         me.roles.relay,
            "recording":     me.roles.recording,
            "file_transfer": me.roles.file_transfer,
            "metrics":       me.roles.metrics,
        },
        "capacity": {
            "max_concurrent_sockets":    me.capacity.max_concurrent_sockets,
            "max_concurrent_rooms":      me.capacity.max_concurrent_rooms,
            "max_audio_participants":    me.capacity.max_audio_participants,
            "max_video_participants":    me.capacity.max_video_participants,
            "max_video_per_room":        me.capacity.max_video_per_room,
            "max_broadcast_subscribers": me.capacity.max_broadcast_subscribers,
            "file_upload_mbps_reserved": me.capacity.file_upload_mbps_reserved,
        },
        "load": {
            "cpu_pct":        me.load.cpu_pct,
            "rss_pct":        me.load.rss_pct,
            "nic_rx_mbps":    me.load.nic_rx_mbps,
            "nic_tx_mbps":    me.load.nic_tx_mbps,
            "active_sockets": me.load.active_sockets,
            "active_rooms":   me.load.active_rooms,
            "active_calls":   me.load.active_calls,
            "phase":          me.load.phase,
        },
        "known_peers": known,
    }


class _RelayBody(BaseModel):
    target_node_id: str
    method: str = "GET"
    path:   str
    body:   Optional[Any] = None
    # Optional fields used by the recursive relay chain. Older callers
    # don't send them; new callers pass the remaining hop budget and
    # the set of node_ids already in the path so we don't loop back.
    _hops_remaining: Optional[int] = None
    _seen_proxies: Optional[list] = None

    class Config:
        extra = "allow"


@router.post("/cluster/relay")
async def cluster_relay(body: _RelayBody):
    """Forward an HTTP request to another node in the mesh.

    Recursive: when the proxy cannot reach the target directly, it
    re-enters `relay_request` itself with a decremented hop budget so
    the chain extends through up to `hops_remaining` Helen-Servers.
    Loops are prevented by the `seen_proxies` set carried with the
    request.
    """
    from app.services.cluster_mesh import relay_request

    # Pull recursive-chain metadata from the request body. The Pydantic
    # model declared them as fields prefixed with underscore so they
    # round-trip cleanly; here we read the raw dict to be tolerant of
    # older callers that don't include them.
    raw = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    hops = int(raw.get("_hops_remaining", 4))
    seen = set(raw.get("_seen_proxies", []) or [])

    status_code, result_body, _headers = await relay_request(
        target_node_id=body.target_node_id,
        method=body.method.upper(),
        path=body.path,
        body=body.body,
        hops_remaining=max(0, hops),
        seen_proxies=seen,
    )
    # Return shape is deliberate: caller inspects the wrapped status.
    return {"status": status_code, "body": result_body}


@router.get("/cluster/members")
async def cluster_members():
    """List every node the mesh currently knows about.

    Public on purpose — lets any peer enumerate the cluster without
    going through the admin auth realm. Exposes nothing sensitive
    (no tokens, no raw DB) — just hardware + load.
    """
    from app.services.node_registry import get_registry
    reg = get_registry()
    return {"nodes": reg.node_dicts(include_dead=True)}


@router.get("/cluster/state-hash")
async def cluster_state_hash():
    """Merkle root + per-table hashes of replicated state.

    Used by the periodic state_reconciliation loop on peer servers
    to detect drift cheaply: a remote peer fetches this, compares
    against its own root, and only requests a full snapshot when
    the roots differ.

    Public for the same reason cluster_members is public — peers on
    the LAN that already passed the federation HMAC gate elsewhere
    use this for convergence; the data exposed is hashes only,
    nothing sensitive.
    """
    from app.services.state_reconciliation import compute_local_state_hash
    return compute_local_state_hash()


@router.get("/cluster/state-snapshot/{table}")
async def cluster_state_snapshot(table: str):
    """Return the full row set for one replicated table so a peer
    that detected a hash mismatch can apply last-write-wins.

    Supported tables:
      * trust        — peer reputation rows
      * sync_policy  — pause flag + blocklist
    """
    if table == "trust":
        from app.services.trust_score import get_trust_db
        return {
            "table": "trust",
            "rows":  get_trust_db().list_top(limit=10_000),
        }
    if table == "sync_policy":
        from app.services.sync_policy import get_sync_policy
        return {
            "table": "sync_policy",
            "rows":  [get_sync_policy().snapshot()],
        }
    raise HTTPException(status_code=404, detail="unknown_table")


@router.get("/cluster/time")
async def cluster_time_endpoint():
    """Current peer-clock + cluster-consensus offset.

    Used by ``cluster_time.py`` on other peers to compute the median
    offset for HMAC signing. Public on purpose — no secret data, just
    a wallclock readout.
    """
    from app.services.cluster_time import get_cluster_time
    import time as _t
    snap = get_cluster_time().snapshot()
    snap["now"] = _t.time()
    return snap


@router.get("/cluster/partition-state")
async def cluster_partition_state():
    """Quorum / split-brain awareness for ops dashboards."""
    from app.services.partition_detector import get_partition_state
    return get_partition_state().snapshot()


@router.get("/cluster/load-balance")
async def cluster_load_balance():
    """Weighted ranking of proxy candidates — useful for debugging
    why the relay chain picked the order it did."""
    from app.services.load_balancer import snapshot
    from app.services.node_registry import get_registry
    reg = get_registry()
    candidates = [n for n in reg.nodes(include_dead=False) if not n.self_node]
    return snapshot(candidates)


class _AntiEntropyDiffBody(BaseModel):
    trust_hashes: dict


@router.post("/cluster/anti-entropy/diff")
async def cluster_anti_entropy_diff(body: _AntiEntropyDiffBody):
    """Compute per-row diff between caller's trust hashes and ours.

    Returns:
      * ``rows_for_you``     — rows the caller should adopt (LWW)
      * ``you_should_push``  — server_ids whose hash differs and we
                               want the caller's copy
    """
    from app.services.anti_entropy import (
        local_trust_hashes, local_trust_rows_by_id, compute_diff,
    )
    local_h = local_trust_hashes()
    push_ids, pull_ids = compute_diff(local_h, body.trust_hashes or {})
    local_rows = local_trust_rows_by_id()
    rows_for_caller = [local_rows[sid] for sid in push_ids if sid in local_rows]
    return {
        "rows_for_you":     rows_for_caller,
        "you_should_push":  pull_ids,
    }


class _AntiEntropyPushBody(BaseModel):
    rows: list[dict]


@router.post("/cluster/anti-entropy/push")
async def cluster_anti_entropy_push(body: _AntiEntropyPushBody):
    """Apply rows the caller pushed to us via LWW."""
    from app.services.anti_entropy import apply_remote_rows
    n = apply_remote_rows(body.rows or [])
    return {"applied": n}


@router.post("/cluster/bandwidth-probe")
async def cluster_bandwidth_probe(request: Request):
    """Bandwidth measurement target — read the body, return 200.

    The caller measures the round-trip time and computes mbps. We
    don't need to do any work other than draining the bytes.
    """
    body = await request.body()
    return {"received": len(body)}


@router.get("/cluster/backpressure")
async def cluster_backpressure():
    """Saturation snapshot — peers in the relay chain consult this
    to route around an overloaded box."""
    from app.services.backpressure import get_backpressure
    return get_backpressure().snapshot()


class _AuditReplicateBody(BaseModel):
    entry: dict


@router.post("/cluster/audit/replicate")
async def cluster_audit_replicate(body: _AuditReplicateBody):
    """Apply an audit entry pushed by a peer (hash-chain validated)."""
    from app.services.audit_replication import get_audit_replicator
    accepted, reason = get_audit_replicator().absorb_remote(body.entry or {})
    return {"accepted": accepted, "reason": reason}


@router.get("/cluster/audit/head")
async def cluster_audit_head():
    """Current head of the local audit chain (seq + last_hash)."""
    from app.services.audit_replication import get_audit_replicator
    return get_audit_replicator().head()


class _ReplicatedPutBody(BaseModel):
    kind:       str
    key:        str
    value:      str
    version:    int
    updated_at: float


@router.post("/cluster/replicated/put")
async def cluster_replicated_put(body: _ReplicatedPutBody):
    """Apply a replicated record from a peer (LWW on (version, ts))."""
    from app.services.replication_manager import absorb_remote
    accepted = absorb_remote(body.model_dump())
    return {"accepted": accepted}


@router.get("/cluster/replicated/{kind}/{key}")
async def cluster_replicated_get(kind: str, key: str):
    """Read one replicated record locally."""
    from app.services.replication_manager import get as rep_get
    rec = rep_get(kind, key)
    if not rec:
        raise HTTPException(status_code=404, detail="not_found")
    return rec


@router.get("/cluster/metrics")
async def cluster_metrics():
    """Prometheus text exposition format — scraped by ops dashboards."""
    from fastapi.responses import PlainTextResponse
    from app.services.metrics_export import render_prometheus
    return PlainTextResponse(
        render_prometheus(),
        media_type="text/plain; version=0.0.4",
    )


# ── Topology graph (public read-only) ───────────────────────────


@router.get("/topology/snapshot")
async def topology_snapshot():
    """Full topology graph snapshot (nodes + links + subnets + stats)."""
    from app.topology import get_topology_manager
    return get_topology_manager().snapshot()


@router.get("/topology/visualize")
async def topology_visualize_ascii():
    """Plain-text ASCII rendering of the current topology."""
    from fastapi.responses import PlainTextResponse
    from app.topology import get_topology_manager
    from app.topology.topology_visualizer import render_ascii
    return PlainTextResponse(
        render_ascii(get_topology_manager().graph),
        media_type="text/plain",
    )


@router.get("/topology/mermaid")
async def topology_visualize_mermaid():
    """Mermaid flowchart syntax — embed in Markdown / docs."""
    from fastapi.responses import PlainTextResponse
    from app.topology import get_topology_manager
    from app.topology.topology_visualizer import render_mermaid
    return PlainTextResponse(
        render_mermaid(get_topology_manager().graph),
        media_type="text/plain",
    )


@router.get("/topology/neighbors/{node_id}")
async def topology_neighbors(node_id: str):
    from app.topology import get_topology_manager
    return {"neighbors": get_topology_manager().neighbors(node_id)}


@router.get("/topology/path/{src}/{dst}")
async def topology_path(src: str, dst: str, k: int = 4):
    from app.topology import get_topology_manager
    return {
        "src":   src,
        "dst":   dst,
        "paths": get_topology_manager().paths(src, dst, k=max(1, min(int(k), 16))),
    }


@router.get("/topology/partitions")
async def topology_partitions():
    from app.topology import get_topology_manager
    return {"components": get_topology_manager().partitions()}


@router.get("/topology/bridges")
async def topology_bridges():
    from app.topology import get_topology_manager
    return {"bridges": get_topology_manager().bridges()}


@router.get("/topology/subnets")
async def topology_subnets():
    from app.topology import get_topology_manager
    return {"subnets": get_topology_manager().subnets()}
