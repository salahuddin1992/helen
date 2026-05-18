"""
app.domains.federation — Inter-server federation, cluster mesh, gossip.

Existing implementation locations:
    app.api.routes.federation         — HMAC-gated inter-server endpoints
    app.api.routes.cluster            — /api/cluster/* peer-facing
    app.api.routes.discovery          — service discovery
    app.api.routes.peers              — LAN peer discovery (public)
    app.services.federation_*         — gateway, router, resilience, shaper, metrics, autodiscovery
    app.services.cluster_*            — mesh, snapshot, time
    app.services.cross_cluster_gossip — gossip protocol
    app.core.federation_auth          — HMAC verify
"""

from __future__ import annotations

from app.domains._safe_import import safe_import

_exports: dict = {}


def _add_router(modpath: str, alias: str) -> None:
    got = safe_import(modpath, ["router"])
    if "router" in got:
        _exports[alias] = got["router"]


_add_router("app.api.routes.federation", "federation_router")
_add_router("app.api.routes.cluster",    "cluster_router")
_add_router("app.api.routes.discovery",  "discovery_router")
_add_router("app.api.routes.peers",      "peers_router")

# Federation services
_exports.update(safe_import(
    "app.services.federation_service",
    ["FederationService", "register_peer", "deliver_envelope"],
))
_exports.update(safe_import(
    "app.services.federation_gateway",
    ["FederationGateway"],
))
_exports.update(safe_import(
    "app.services.federation_router",
    ["FederationRouter", "route_message"],
))
_exports.update(safe_import(
    "app.services.federation_resilience",
    ["ResiliencePolicy", "retry_with_backoff"],
))
_exports.update(safe_import(
    "app.services.federation_shaper",
    ["TrafficShaper"],
))
_exports.update(safe_import(
    "app.services.federation_metrics",
    ["FederationMetrics", "record_inbound", "record_outbound"],
))
_exports.update(safe_import(
    "app.services.federation_autodiscovery",
    ["FederationAutoDiscovery"],
))

# Cluster services
_exports.update(safe_import(
    "app.services.cluster_mesh",
    ["ClusterMesh", "join_cluster", "leave_cluster"],
))
_exports.update(safe_import(
    "app.services.cluster_snapshot",
    ["ClusterSnapshot", "take_snapshot"],
))
_exports.update(safe_import(
    "app.services.cluster_time",
    ["ClusterClock", "now_ms"],
))
_exports.update(safe_import(
    "app.services.cross_cluster_gossip",
    ["CrossClusterGossip"],
))

# HMAC core
_exports.update(safe_import(
    "app.core.federation_auth",
    ["verify_federation_hmac", "sign_federation_request"],
))

# Models
_exports.update(safe_import("app.models.server_node", ["ServerNode"]))

globals().update(_exports)
__all__ = sorted(_exports.keys())
