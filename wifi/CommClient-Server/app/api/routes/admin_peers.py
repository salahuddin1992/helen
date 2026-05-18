"""
Admin APIs for peer approval workflow.

All endpoints require admin role. Approvals/rejections/denials write
to the audit log. Cluster-mismatched peers cannot be approved over.

Routes
------
::

    GET  /api/admin/peers/discovered
    GET  /api/admin/peers/pending
    GET  /api/admin/peers/approved
    GET  /api/admin/peers/rejected
    GET  /api/admin/peers/denied

    POST /api/admin/peers/{server_id}/approve
    POST /api/admin/peers/{server_id}/reject              {reason: str}
    POST /api/admin/peers/{server_id}/deny                {reason: str}
    POST /api/admin/peers/{server_id}/ignore
    POST /api/admin/peers/{server_id}/trust-permanently
    POST /api/admin/peers/{server_id}/trust-once
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.user import User
from app.services.peer_approval_service import (
    PeerApprovalError,
    peer_approval_service,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/peers", tags=["admin-peers"])


async def _require_admin(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> str:
    user = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if user is None or getattr(user, "role", None) != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return user_id


# ── Schemas ────────────────────────────────────────────────────────


class ReasonRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=512)


class PeerListResponse(BaseModel):
    peers: list[dict]
    count: int


class PeerActionResponse(BaseModel):
    ok: bool
    peer: dict


# ── List endpoints ─────────────────────────────────────────────────


@router.get("/discovered", response_model=PeerListResponse)
async def list_discovered(
    user_id: str = Depends(_require_admin),
    limit: int = Query(default=200, ge=1, le=1000),
):
    peers = await peer_approval_service.list_discovered_peers(limit=limit)
    return {"peers": peers, "count": len(peers)}


@router.get("/pending", response_model=PeerListResponse)
async def list_pending(
    user_id: str = Depends(_require_admin),
    limit: int = Query(default=200, ge=1, le=1000),
):
    peers = await peer_approval_service.list_pending_peers(limit=limit)
    return {"peers": peers, "count": len(peers)}


@router.get("/approved", response_model=PeerListResponse)
async def list_approved(
    user_id: str = Depends(_require_admin),
    limit: int = Query(default=200, ge=1, le=1000),
):
    peers = await peer_approval_service.list_approved_peers(limit=limit)
    return {"peers": peers, "count": len(peers)}


@router.get("/rejected", response_model=PeerListResponse)
async def list_rejected(
    user_id: str = Depends(_require_admin),
    limit: int = Query(default=200, ge=1, le=1000),
):
    peers = await peer_approval_service.list_rejected_peers(limit=limit)
    return {"peers": peers, "count": len(peers)}


@router.get("/denied", response_model=PeerListResponse)
async def list_denied(
    user_id: str = Depends(_require_admin),
    limit: int = Query(default=200, ge=1, le=1000),
):
    peers = await peer_approval_service.list_denied_peers(limit=limit)
    return {"peers": peers, "count": len(peers)}


# ── Action endpoints ───────────────────────────────────────────────


@router.post("/{server_id}/approve", response_model=PeerActionResponse)
async def approve(
    server_id: str,
    user_id: str = Depends(_require_admin),
):
    try:
        result = await peer_approval_service.approve_peer(server_id, user_id)
    except PeerApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "peer": result}


@router.post("/{server_id}/reject", response_model=PeerActionResponse)
async def reject(
    server_id: str,
    body: ReasonRequest,
    user_id: str = Depends(_require_admin),
):
    try:
        result = await peer_approval_service.reject_peer(
            server_id, user_id, body.reason,
        )
    except PeerApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "peer": result}


@router.post("/{server_id}/deny", response_model=PeerActionResponse)
async def deny(
    server_id: str,
    body: ReasonRequest,
    user_id: str = Depends(_require_admin),
):
    try:
        result = await peer_approval_service.deny_peer(
            server_id, user_id, body.reason,
        )
    except PeerApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "peer": result}


@router.post("/{server_id}/ignore", response_model=PeerActionResponse)
async def ignore(
    server_id: str,
    user_id: str = Depends(_require_admin),
):
    try:
        result = await peer_approval_service.ignore_peer(server_id, user_id)
    except PeerApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "peer": result}


@router.post("/{server_id}/trust-permanently", response_model=PeerActionResponse)
async def trust_permanently(
    server_id: str,
    user_id: str = Depends(_require_admin),
):
    try:
        result = await peer_approval_service.trust_peer_permanently(server_id, user_id)
    except PeerApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "peer": result}


@router.post("/{server_id}/trust-once", response_model=PeerActionResponse)
async def trust_once(
    server_id: str,
    user_id: str = Depends(_require_admin),
):
    try:
        result = await peer_approval_service.trust_peer_once(server_id, user_id)
    except PeerApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "peer": result}


# ── Sync-policy (live federation kill-switch + blocklist) ──────────


class SyncPolicyResponse(BaseModel):
    paused: bool
    blocked_server_ids: list[str]
    loaded_at: float


class SyncPolicyUpdate(BaseModel):
    paused: Optional[bool] = None


@router.get("/sync-policy", response_model=SyncPolicyResponse)
async def get_sync_policy_endpoint(
    user_id: str = Depends(_require_admin),
):
    """Return the current cluster-sync policy.

    `paused=true` parks newly discovered peers in WAITING_MANUAL_APPROVAL.
    `blocked_server_ids` is the hard-deny list checked at the HMAC gate.
    """
    from app.services.sync_policy import get_sync_policy
    return get_sync_policy().snapshot()


@router.post("/sync-policy", response_model=SyncPolicyResponse)
async def set_sync_policy_endpoint(
    body: SyncPolicyUpdate,
    user_id: str = Depends(_require_admin),
):
    """Toggle the master federation kill-switch.

    POST {"paused": true}  → new peers wait for manual approval.
    POST {"paused": false} → resume auto_accept (federate-first).
    """
    from app.services.sync_policy import get_sync_policy
    policy = get_sync_policy()
    if body.paused is not None:
        return policy.set_paused(body.paused)
    return policy.snapshot()


@router.post("/{server_id}/block", response_model=SyncPolicyResponse)
async def block_peer_endpoint(
    server_id: str,
    user_id: str = Depends(_require_admin),
):
    """Hard-block a peer at the federation HMAC gate.

    Blocked peers are rejected on every endpoint including discovery
    probes. Combine with /deny if you also want to clear approval state.
    """
    from app.services.sync_policy import get_sync_policy
    try:
        return get_sync_policy().block(server_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{server_id}/unblock", response_model=SyncPolicyResponse)
async def unblock_peer_endpoint(
    server_id: str,
    user_id: str = Depends(_require_admin),
):
    """Remove a peer from the sync-policy blocklist."""
    from app.services.sync_policy import get_sync_policy
    try:
        return get_sync_policy().unblock(server_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Path-health diagnostics ────────────────────────────────────────


@router.get("/path-health")
async def path_health_endpoint(
    user_id: str = Depends(_require_admin),
):
    """Live latency + failure-cooldown stats for every (host:port)
    the relay chain has touched. Used by the admin UI to visualise
    which links are fast, slow, or in cooldown."""
    from app.services.path_health import get_path_health
    return get_path_health().snapshot()


# ── Trust score (persistent peer reputation) ─────────────────────


@router.get("/trust")
async def list_trust_scores(
    user_id: str = Depends(_require_admin),
    limit: int = Query(default=100, ge=1, le=1000),
    ascending: bool = Query(default=False),
):
    """List peers ordered by reputation. ascending=true puts the
    worst-behaved peers first (useful for finding candidates for a
    permanent block)."""
    from app.services.trust_score import get_trust_db
    return {"peers": get_trust_db().list_top(limit=limit, ascending=ascending)}


@router.get("/trust/{server_id}")
async def get_trust_score(
    server_id: str,
    user_id: str = Depends(_require_admin),
):
    from app.services.trust_score import get_trust_db
    return get_trust_db().get(server_id)


@router.post("/trust/{server_id}/reset")
async def reset_trust_score(
    server_id: str,
    user_id: str = Depends(_require_admin),
):
    """Wipe a peer's reputation history (manual rehabilitation)."""
    from app.services.trust_score import get_trust_db
    return get_trust_db().reset(server_id)


# ── mDNS discovery status ────────────────────────────────────────


@router.get("/mdns")
async def mdns_status(
    user_id: str = Depends(_require_admin),
):
    """Return whether mDNS discovery is registered and listening."""
    from app.services.mdns_discovery import status as mdns_status_fn
    return mdns_status_fn()


# ── State reconciliation diagnostics ─────────────────────────────


@router.get("/reconciliation/state-hash")
async def reconciliation_state_hash(
    user_id: str = Depends(_require_admin),
):
    """Local Merkle hashes of replicated tables. Useful for spotting
    drift before the periodic loop catches it."""
    from app.services.state_reconciliation import compute_local_state_hash
    return compute_local_state_hash()


# ── Cluster-time / partition / load-balance diagnostics ─────────


@router.get("/cluster-time")
async def cluster_time_admin(
    user_id: str = Depends(_require_admin),
):
    """Cluster-consensus time + offset from this host's wall clock."""
    from app.services.cluster_time import get_cluster_time
    return get_cluster_time().snapshot()


@router.get("/partition-state")
async def partition_state_admin(
    user_id: str = Depends(_require_admin),
):
    """Quorum / split-brain status for the local node."""
    from app.services.partition_detector import get_partition_state
    return get_partition_state().snapshot()


@router.get("/load-balance")
async def load_balance_admin(
    user_id: str = Depends(_require_admin),
):
    """Weighted ranking of proxy candidates with full breakdown."""
    from app.services.load_balancer import snapshot
    from app.services.node_registry import get_registry
    reg = get_registry()
    return snapshot([n for n in reg.nodes(include_dead=False) if not n.self_node])


# ── Bandwidth / Backpressure / Audit admin views ────────────────


@router.get("/bandwidth")
async def bandwidth_admin(
    user_id: str = Depends(_require_admin),
):
    """Per-(host:port) measured throughput."""
    from app.services.bandwidth_probe import get_bandwidth
    return get_bandwidth().snapshot()


@router.get("/backpressure")
async def backpressure_admin(
    user_id: str = Depends(_require_admin),
):
    """Live saturation level + inputs that drive the gate."""
    from app.services.backpressure import get_backpressure
    return get_backpressure().snapshot()


@router.get("/audit/head")
async def audit_head_admin(
    user_id: str = Depends(_require_admin),
):
    """Current head of the local hash-chained audit log."""
    from app.services.audit_replication import get_audit_replicator
    return get_audit_replicator().head()


@router.get("/audit/verify")
async def audit_verify_admin(
    user_id: str = Depends(_require_admin),
    max_entries: int = Query(default=10_000, ge=1, le=1_000_000),
):
    """Walk the audit chain and verify every entry's hash linkage.

    Returns ok=False with broken_at = seq number of first mismatch.
    """
    from app.services.audit_replication import get_audit_replicator
    return get_audit_replicator().verify_chain(max_entries=max_entries)


# ── Phi accrual / adaptive timeout / hash ring / bloom views ────


@router.get("/phi-accrual")
async def phi_accrual_admin(
    user_id: str = Depends(_require_admin),
):
    """Per-peer suspicion levels (φ values) from the accrual detector."""
    from app.services.phi_accrual import get_phi_registry
    return get_phi_registry().snapshot()


@router.get("/adaptive-timeout")
async def adaptive_timeout_admin(
    user_id: str = Depends(_require_admin),
):
    """Per-peer SRTT / RTTVAR / RTO derived via RFC 6298 from live RTTs."""
    from app.services.adaptive_timeout import get_adaptive_timeout
    return get_adaptive_timeout().snapshot()


@router.get("/hash-ring")
async def hash_ring_admin(
    user_id: str = Depends(_require_admin),
    sample_keys: int = Query(default=10_000, ge=100, le=100_000),
):
    """Consistent-hash ring keyspace distribution (sampled)."""
    from app.services.consistent_hash import get_ring, refresh_from_registry
    refresh_from_registry()
    ring = get_ring()
    return {
        "peer_count":  ring.peer_count(),
        "vnode_count": ring.vnode_count(),
        "keyspace":    ring.keyspace_share(sample_keys=sample_keys),
    }


@router.get("/bloom-stats")
async def bloom_stats_admin(
    user_id: str = Depends(_require_admin),
):
    """Bloom filter occupancy / FPR for the current peer set."""
    from app.services.bloom_discovery import build_local_peer_filter
    return build_local_peer_filter().stats()


@router.get("/routing-strategy")
async def routing_strategy_admin(
    user_id: str = Depends(_require_admin),
):
    """Live routing-strategy state: active policy, metrics, last
    decision trace, available strategies."""
    from app.routing_strategy import get_strategy_manager
    return get_strategy_manager().snapshot()


@router.get("/distributed-system")
async def distributed_system_admin(
    user_id: str = Depends(_require_admin),
):
    """Top-level distributed-system snapshot: identity, capabilities,
    lifecycle, membership, partition state, replication + consensus
    stats, and the recent event history."""
    from app.distributed_system import get_distributed_manager
    return get_distributed_manager().snapshot()


@router.get("/monitoring/dashboard")
async def monitoring_dashboard_admin(
    user_id: str = Depends(_require_admin),
):
    """Aggregated monitoring snapshot — health + metrics + alerts +
    latency + topology + recent events."""
    from app.monitoring import get_monitoring_manager
    return get_monitoring_manager().snapshot()


@router.get("/monitoring/dashboard.txt")
async def monitoring_dashboard_text_admin(
    user_id: str = Depends(_require_admin),
):
    """Plain-text dashboard for terminals + log shipping."""
    from fastapi.responses import PlainTextResponse
    from app.monitoring.dashboard_renderer import render_text
    return PlainTextResponse(render_text(), media_type="text/plain")


@router.get("/monitoring/health")
async def monitoring_health_admin(
    user_id: str = Depends(_require_admin),
):
    """Latest health-check snapshot."""
    from app.monitoring.health_checker import get_health_checker
    return get_health_checker().latest()


@router.get("/monitoring/alerts")
async def monitoring_alerts_admin(
    user_id: str = Depends(_require_admin),
):
    """Per-alert state + transition history."""
    from app.monitoring.alert_manager import get_alert_manager
    return get_alert_manager().all_states()


@router.get("/monitoring/latency")
async def monitoring_latency_admin(
    user_id: str = Depends(_require_admin),
):
    """Per-op latency histograms (count / mean / p95 / p99)."""
    from app.monitoring.latency_tracker import get_latency_tracker
    return get_latency_tracker().all_stats()


@router.get("/p2p/snapshot")
async def p2p_snapshot_admin(
    user_id: str = Depends(_require_admin),
):
    """Top-level P2P snapshot — registry + selection + gossip + DHT
    + bridge + federation + NAT + sessions + relay stats + events."""
    from app.p2p import get_p2p_manager
    return get_p2p_manager().snapshot()


@router.get("/p2p/peers")
async def p2p_peers_admin(
    user_id: str = Depends(_require_admin),
):
    """Full p2p-layer peer list."""
    from app.p2p.peer_registry import get_p2p_registry
    return {"peers": [p.to_dict() for p in get_p2p_registry().all()]}


@router.get("/p2p/selection")
async def p2p_selection_admin(
    user_id: str = Depends(_require_admin),
):
    """Per-purpose peer ranking (top overall / relays / bridges)."""
    from app.p2p.peer_selection import selection_snapshot
    return selection_snapshot()


@router.get("/overlay/snapshot")
async def overlay_snapshot_admin(
    user_id: str = Depends(_require_admin),
):
    """Overlay manager snapshot — registry + sessions + recent events."""
    from app.overlay import get_overlay_manager
    return get_overlay_manager().snapshot()


@router.get("/overlay/{name}")
async def overlay_inspect_admin(
    name: str,
    user_id: str = Depends(_require_admin),
):
    """Per-overlay full graph dump (nodes + links)."""
    from app.overlay.overlay_registry import get_overlay_registry
    g = get_overlay_registry().get(name)
    if g is None:
        raise HTTPException(status_code=404, detail="overlay_not_found")
    return g.to_dict()


@router.get("/resilience/snapshot")
async def resilience_snapshot_admin(
    user_id: str = Depends(_require_admin),
):
    """Resilience manager snapshot — degraded mode + breakers +
    retry queue + recovery stats + recent events."""
    from app.resilience import get_resilience_manager
    return get_resilience_manager().snapshot()


@router.get("/nat/snapshot")
async def nat_snapshot_admin(
    user_id: str = Depends(_require_admin),
):
    """NAT manager snapshot — detected NAT type + strategies +
    rendezvous + sessions + recent events."""
    from app.nat import get_nat_manager
    return get_nat_manager().snapshot()


@router.get("/e2ee/status")
async def e2ee_status_admin(
    user_id: str = Depends(_require_admin),
):
    """E2EE wrapper counters — enabled flag + encrypted/plain
    fallback counts. Set HELEN_E2EE_ENABLED=1 to activate."""
    from app.services.e2ee_message_wrapper import status
    return status()


@router.get("/federation/gateway/status")
async def federation_gateway_status_admin(
    user_id: str = Depends(_require_admin),
):
    """List the cross-cluster routes the gateway can re-sign for.
    Configure via HELEN_FEDERATED_CLUSTERS=cluster:secret,..."""
    from app.services.federation_gateway import status
    return status()


@router.get("/alerts/webhooks/status")
async def alerts_webhooks_status_admin(
    user_id: str = Depends(_require_admin),
):
    """Webhook dispatcher counters. Configure URLs via
    HELEN_ALERT_WEBHOOKS=url1,url2."""
    from app.monitoring.webhook_dispatcher import status
    return status()


class _RelayTraceBody(BaseModel):
    target_node_id: str
    method: str = "GET"
    path:   str = "/api/cluster/info"
    hops_remaining: int = 4


@router.post("/relay/trace")
async def relay_trace_admin(
    body: _RelayTraceBody,
    user_id: str = Depends(_require_admin),
):
    """Run a single relay attempt; returns hop chain + status."""
    from app.services.relay_diagnostics import trace
    return await trace(
        target_node_id=body.target_node_id,
        method=body.method, path=body.path,
        hops_remaining=body.hops_remaining,
    )


@router.get("/relay/chain")
async def relay_chain_admin(
    user_id: str = Depends(_require_admin),
):
    """Visualize relay chain capabilities + known bridges."""
    from app.services.relay_diagnostics import chain_visualizer
    return chain_visualizer()


@router.post("/recovery/force-heal")
async def recovery_force_heal_admin(
    user_id: str = Depends(_require_admin),
):
    """Manually trigger gossip + reconciliation + bandwidth probe +
    partition recheck."""
    from app.services.force_heal import force_heal_now
    return await force_heal_now()


class _OverlayCreateBody(BaseModel):
    name: str


@router.post("/overlay/create")
async def overlay_create_admin(
    body: _OverlayCreateBody,
    user_id: str = Depends(_require_admin),
):
    """Create a new logical overlay by name."""
    from app.overlay import get_overlay_manager
    g = get_overlay_manager().create_overlay(body.name)
    return g.to_dict()


@router.delete("/overlay/{name}")
async def overlay_delete_admin(
    name: str,
    user_id: str = Depends(_require_admin),
):
    """Drop an overlay (graph + sessions)."""
    from app.overlay import get_overlay_manager
    return {"dropped": get_overlay_manager().drop_overlay(name)}


class _OverlayNodeBody(BaseModel):
    overlay_name: str
    node_id:      str
    peer_id:      str = ""
    tags:         list[str] = []


@router.post("/overlay/node")
async def overlay_add_node_admin(
    body: _OverlayNodeBody,
    user_id: str = Depends(_require_admin),
):
    """Add a node to an existing overlay."""
    from app.overlay import get_overlay_manager
    n = get_overlay_manager().add_node(
        body.overlay_name, body.node_id,
        peer_id=body.peer_id, tags=set(body.tags),
    )
    return n.to_dict()


class _OverlayLinkBody(BaseModel):
    overlay_name:   str
    src_id:         str
    dst_id:         str
    weight:         float = 1.0
    bidirectional:  bool = False


@router.post("/overlay/link")
async def overlay_add_link_admin(
    body: _OverlayLinkBody,
    user_id: str = Depends(_require_admin),
):
    """Add a link to an overlay (optionally bidirectional)."""
    from app.overlay import get_overlay_manager
    L = get_overlay_manager().add_link(
        body.overlay_name, body.src_id, body.dst_id,
        weight=body.weight, bidirectional=body.bidirectional,
    )
    return L.to_dict()


# ── Round 2 admin endpoints ──────────────────────────────────


@router.get("/cross-cluster/gossip/status")
async def cross_cluster_gossip_status_admin(
    user_id: str = Depends(_require_admin),
):
    """Cross-cluster gossip stats — cycles + peers learned + blocklist sync count."""
    from app.services.cross_cluster_gossip import status
    return status()


@router.get("/anomaly/snapshot")
async def anomaly_snapshot_admin(
    user_id: str = Depends(_require_admin),
):
    """Anomaly detector — tracked metrics, last anomalies, sample counts."""
    from app.services.anomaly_detector import get_anomaly_detector
    return get_anomaly_detector().snapshot()


@router.get("/capacity/forecast")
async def capacity_forecast_admin(
    user_id: str = Depends(_require_admin),
):
    """Per-metric saturation ETA based on rolling trend."""
    from app.services.capacity_planner import get_capacity_planner
    p = get_capacity_planner()
    p.tick()
    return p.forecast()


@router.get("/http-pool/snapshot")
async def http_pool_snapshot_admin(
    user_id: str = Depends(_require_admin),
):
    """HTTP keep-alive pool — open clients + per-client request counts."""
    from app.services.http_connection_pool import get_pool
    return get_pool().snapshot()


@router.get("/screen-share/presets")
async def screen_share_presets_admin(
    user_id: str = Depends(_require_admin),
):
    """Available screen-share quality presets."""
    from app.services.screen_share_quality import all_presets
    return {"presets": all_presets()}


@router.get("/e2ee/sessions")
async def e2ee_sessions_admin(
    user_id: str = Depends(_require_admin),
):
    """E2EE session registry — active sessions + rotation hints."""
    from app.services.e2ee_session_manager import get_e2ee_session_registry
    return get_e2ee_session_registry().snapshot()


class _OverlayTemplateBody(BaseModel):
    template:    str       # ring / star / tree / mesh / topic
    name:        str
    peer_ids:    list[str]
    hub:         str = ""
    branching:   int = 2
    topic:       str = ""


@router.post("/overlay/template")
async def overlay_template_admin(
    body: _OverlayTemplateBody,
    user_id: str = Depends(_require_admin),
):
    """Build a pre-shaped overlay (ring/star/tree/mesh/topic)."""
    from app.overlay import overlay_templates as t
    if body.template == "ring":
        return t.build_ring(body.name, body.peer_ids)
    if body.template == "star":
        if not body.hub or not body.peer_ids:
            return {"ok": False, "error": "star_needs_hub_and_leaves"}
        return t.build_star(body.name, body.hub, body.peer_ids)
    if body.template == "tree":
        return t.build_tree(body.name, body.hub or body.peer_ids[0],
                            body.peer_ids, branching=body.branching)
    if body.template == "mesh":
        return t.build_full_mesh(body.name, body.peer_ids)
    if body.template == "topic":
        return t.build_topic(body.name, body.topic, body.peer_ids,
                             hub=body.hub or None)
    return {"ok": False, "error": f"unknown_template:{body.template}"}


# ── Round 3 admin endpoints ──────────────────────────────────


@router.get("/compression/status")
async def compression_status_admin(
    user_id: str = Depends(_require_admin),
):
    """Compression layer counters (zlib for relay payloads)."""
    from app.services.compression_layer import status
    return status()


@router.get("/qos/snapshot")
async def qos_snapshot_admin(
    user_id: str = Depends(_require_admin),
):
    """QoS traffic-shaper bucket state per traffic class."""
    from app.services.qos_traffic_shaper import get_shaper
    return get_shaper().snapshot()


@router.get("/rate-limit/snapshot")
async def rate_limit_snapshot_admin(
    user_id: str = Depends(_require_admin),
):
    """Distributed rate-limiter local cache."""
    from app.services.distributed_rate_limiter import snapshot
    return snapshot()


@router.get("/tracing/recent")
async def tracing_recent_admin(
    user_id: str = Depends(_require_admin),
    limit: int = Query(default=50, ge=1, le=500),
):
    """Recent completed spans (in-memory ring)."""
    from app.services.distributed_tracing import get_tracing
    return {"spans": get_tracing().recent(limit)}


@router.get("/tracing/{trace_id}")
async def tracing_by_id_admin(
    trace_id: str,
    user_id: str = Depends(_require_admin),
):
    """All spans for a single trace_id (chronological)."""
    from app.services.distributed_tracing import get_tracing
    return {"spans": get_tracing().by_trace(trace_id)}


@router.get("/metrics-history/stats")
async def metrics_history_stats_admin(
    user_id: str = Depends(_require_admin),
):
    """Time-series store stats — count + retention + DB path."""
    from app.services.metrics_history import get_metrics_history
    return get_metrics_history().stats()


class _MessageSearchBody(BaseModel):
    query:     str
    room_id:   str = ""
    sender_id: str = ""
    limit:     int = 50


@router.post("/message-search")
async def message_search_admin(
    body: _MessageSearchBody,
    user_id: str = Depends(_require_admin),
):
    """FTS5 search across indexed messages."""
    from app.services.message_search_index import get_message_search
    return {
        "results": get_message_search().search(
            body.query,
            room_id=body.room_id or None,
            sender_id=body.sender_id or None,
            limit=body.limit,
        ),
    }


@router.get("/saga/list")
async def saga_list_admin(
    user_id: str = Depends(_require_admin),
    limit: int = Query(default=50, ge=1, le=500),
):
    """List sagas (newest first)."""
    from app.services.saga_engine import get_saga_engine
    return {"sagas": get_saga_engine().list(limit=limit)}


@router.get("/saga/stats")
async def saga_stats_admin(
    user_id: str = Depends(_require_admin),
):
    """Saga engine stats — counts by status + registered steps."""
    from app.services.saga_engine import get_saga_engine
    return get_saga_engine().stats()


@router.get("/affinity/snapshot")
async def affinity_snapshot_admin(
    user_id: str = Depends(_require_admin),
):
    """Session-affinity cache — user_id → home_node bindings."""
    from app.services.session_affinity import snapshot
    return snapshot()


# ── Round 5 admin endpoints ──────────────────────────────────


class _FlagBody(BaseModel):
    name:           str
    enabled:        bool = False
    rollout_pct:    int = 0
    allowed_users:  list[str] = []
    blocked_users:  list[str] = []
    description:    str = ""


@router.get("/feature-flags")
async def feature_flags_list_admin(
    user_id: str = Depends(_require_admin),
):
    """List cached feature flags."""
    from app.services.feature_flags import get_flag_store
    return get_flag_store().snapshot()


@router.post("/feature-flags")
async def feature_flags_set_admin(
    body: _FlagBody,
    user_id: str = Depends(_require_admin),
):
    """Set or update a feature flag (cluster-wide)."""
    from app.services.feature_flags import Flag, get_flag_store
    flag = Flag(
        name=body.name, enabled=body.enabled,
        rollout_pct=body.rollout_pct,
        allowed_users=body.allowed_users,
        blocked_users=body.blocked_users,
        description=body.description,
    )
    get_flag_store().set(flag)
    return {"ok": True, "flag": flag.to_dict()}


@router.get("/cluster-snapshot/capture")
async def cluster_snapshot_capture_admin(
    user_id: str = Depends(_require_admin),
    save: bool = Query(default=False),
    label: str = Query(default=""),
):
    """Capture a point-in-time cluster snapshot (optionally save)."""
    from app.services.cluster_snapshot import capture, save as save_snap
    snap = capture()
    saved_path = None
    if save:
        path = save_snap(snap, label=label)
        saved_path = str(path) if path else None
    return {"snapshot": snap, "saved_to": saved_path}


@router.get("/cluster-snapshot/list")
async def cluster_snapshot_list_admin(
    user_id: str = Depends(_require_admin),
):
    """List on-disk snapshot files."""
    from app.services.cluster_snapshot import list_snapshots
    return {"snapshots": list_snapshots()}


@router.get("/backup-encryption/status")
async def backup_encryption_status_admin(
    user_id: str = Depends(_require_admin),
):
    """Backup encryption availability + key fingerprint."""
    from app.services.backup_encryption import status
    return status()


@router.get("/turn/credentials")
async def turn_credentials_admin(
    user_id: str = Depends(_require_admin),
    target_user_id: str = Query(default=""),
    ttl_sec: int = Query(default=3600, ge=60, le=86400),
):
    """Issue WebRTC ICE-server config (STUN + TURN credentials)."""
    from app.services.turn_allocator import ice_servers_for, status
    return {
        "ice_servers": ice_servers_for(target_user_id, ttl_sec=ttl_sec),
        "config":      status(),
    }


@router.post("/config/reload")
async def config_reload_admin(
    user_id: str = Depends(_require_admin),
):
    """Re-read env-tunable settings across all subsystems."""
    from app.services.config_hot_reload import reload_all
    return reload_all()


@router.post("/plugins/reload")
async def plugins_reload_admin(
    user_id: str = Depends(_require_admin),
):
    """Re-scan the plugin directory and rebuild the hook table."""
    from app.services.plugin_loader import get_plugins
    return get_plugins().load_all()


@router.get("/plugins/snapshot")
async def plugins_snapshot_admin(
    user_id: str = Depends(_require_admin),
):
    """List loaded plugins + registered hooks."""
    from app.services.plugin_loader import get_plugins
    return get_plugins().snapshot()


@router.get("/multipath-routes")
async def multipath_routes_admin(
    user_id: str = Depends(_require_admin),
):
    """Full multi-path route-table dump with live scores + breakdown.

    Each row shows: target_node_id, route_type, hops, score, cooldown
    state, consecutive_failures, last_success_age_s, and the per-factor
    breakdown that produced the score. Use this to debug why a
    particular route was picked (or rejected)."""
    from app.services.multipath_router import snapshot
    return snapshot()


@router.get("/locks/{name}")
async def lock_status_admin(
    name: str,
    user_id: str = Depends(_require_admin),
):
    """Current owner + TTL for a named distributed lock."""
    from app.services.distributed_lock import lock_status
    return lock_status(name)


@router.get("/audit/archive")
async def audit_archive_admin(
    user_id: str = Depends(_require_admin),
):
    """Compaction archive index — months + merkle roots."""
    from app.services.log_compaction import archive_summary
    return archive_summary()


@router.post("/audit/compact-now")
async def audit_compact_now_admin(
    user_id: str = Depends(_require_admin),
):
    """Trigger compaction immediately (gated by the cluster-wide
    lock so only one peer actually runs it)."""
    from app.services.distributed_lock import distributed_lock
    from app.services.log_compaction import compact_once
    async with distributed_lock("log_compactor", ttl=600.0,
                                 acquire_timeout=2.0) as held:
        if not held:
            return {"ran": False, "reason": "lock_held_elsewhere"}
        return {"ran": True, **compact_once()}
