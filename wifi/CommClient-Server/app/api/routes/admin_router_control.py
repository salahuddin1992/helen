"""
Server-side proxy router for the Helen-Router admin API.

This router lives on Helen-Server (port 3000) and forwards admin
calls to the configured Helen-Router instance (default
``http://router.helen.lan:8080``). Operators authenticate to
Helen-Server with the normal admin JWT; the proxy injects the
router's shared bearer token on the outbound leg.

Mount point
-----------
  ``/api/admin/router/*``
  ``/api/admin/mesh/*``

Every endpoint enforces ``require_role("admin")``. Every write op
goes through :class:`RouterAuditHook` so the change appears in
the same SIEM dashboard as application-level changes.

Endpoint inventory
------------------

  Overview / health
      GET    /router/health
      GET    /router/upstreams
      GET    /router/reachability

  Mesh overlay
      GET    /mesh/topology
      GET    /mesh/neighbours
      POST   /mesh/reroute
      DELETE /mesh/neighbours/{id}

  Service registry
      GET    /router/register
      POST   /router/register
      PUT    /router/register/{id}
      DELETE /router/register/{id}

  Reverse-proxy controls
      GET    /router/proxy/log
      GET    /router/proxy/rate-limits
      POST   /router/proxy/rate-limits
      GET    /router/proxy/ip-lists/{list_name}
      POST   /router/proxy/ip-lists/{list_name}

  DNS
      GET    /router/dns/records
      POST   /router/dns/records
      DELETE /router/dns/records
      GET    /router/dns/blocklist
      POST   /router/dns/blocklist
      GET    /router/dns/stats
      GET    /router/dns/log
      GET    /router/dns/upstreams
      POST   /router/dns/upstreams

  NTP
      GET    /router/ntp/status
      POST   /router/ntp/sync

  UPnP
      GET    /router/upnp/portmaps
      POST   /router/upnp/portmap
      DELETE /router/upnp/portmap/{id}
      POST   /router/upnp/discover

  Vendor adapters
      POST   /router/vendor/test
      POST   /router/vendor/push
      GET    /router/vendor/jobs

  External routers
      GET    /router/external
      POST   /router/external/scan

  Connection broker
      GET    /router/broker/status

  Security
      GET    /router/security
      POST   /router/security/rotate-token
      GET    /router/security/subnets
      POST   /router/security/subnets
      POST   /router/security/enforcement

  Diagnostics
      POST   /router/diag/ping
      POST   /router/diag/traceroute
      POST   /router/diag/dns
      POST   /router/diag/portscan
      POST   /router/diag/bandwidth

  Config + admin
      GET    /router/config
      PUT    /router/config
      POST   /router/config/validate
      POST   /router/admin/reload
      POST   /router/admin/restart
      GET    /router/control/connection      (Helen-Server local)
      PUT    /router/control/connection      (Helen-Server local)

  Routing rules
      GET    /router/routing/rules
      POST   /router/routing/rules
      DELETE /router/routing/rules
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.core.security_utils import require_role
from app.services.router_control import (
    RouterAuditHook,
    RouterProxyHandler,
    get_router_client,
    get_router_config_store,
)

logger = get_logger(__name__)


router = APIRouter(prefix="/admin", tags=["admin-router-control"])


# ── DI for require_admin (alias matches spec) ────────────────────


def require_admin():
    """Dependency factory — equivalent to ``require_role('admin')``."""
    return require_role("admin")


# ── Per-request handler factory ──────────────────────────────────


async def _handler() -> RouterProxyHandler:
    client = await get_router_client()
    return RouterProxyHandler(client=client, audit=RouterAuditHook())


# ── Pydantic models for local (non-proxied) endpoints ────────────


class RouterControlConnectionUpdate(BaseModel):
    """Body for PUT /api/admin/router/control/connection."""

    base_url: str = Field(..., min_length=1, max_length=512)
    token: Optional[str] = Field(
        None,
        description=(
            "If null, keep existing token; if empty string, clear it; "
            "otherwise set the new token."
        ),
    )


# =====================================================================
# Helen-Server-LOCAL endpoints — manage the proxy itself, not forwarded
# =====================================================================


@router.get("/router/control/connection")
async def get_connection_config(
    admin: str = Depends(require_admin()),
) -> dict[str, Any]:
    """Return the resolved (URL + token-set flag) the proxy is using.

    Note: never echoes back the actual token bytes — only the
    ``token_set`` boolean and length.
    """
    store = get_router_config_store()
    cfg = await store.get()
    audit_log(
        "router.control.connection.viewed",
        user_id=admin, success=True,
    )
    return cfg.sanitized()


@router.put("/router/control/connection")
async def set_connection_config(
    body: RouterControlConnectionUpdate,
    admin: str = Depends(require_admin()),
) -> dict[str, Any]:
    """Override the router URL + token at runtime (DB-backed)."""
    store = get_router_config_store()
    try:
        cfg = await store.set_override(body.base_url, body.token)
    except Exception as exc:
        audit_log(
            "router.control.connection.updated",
            user_id=admin, success=False,
            details={"error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    audit_log(
        "router.control.connection.updated",
        user_id=admin, success=True,
        details={
            "base_url": cfg.base_url,
            "token_set": cfg.has_token,
        },
    )
    return cfg.sanitized()


@router.get("/router/reachability")
async def reachability_probe(
    admin: str = Depends(require_admin()),
) -> dict[str, Any]:
    """Quick yes/no — is Helen-Router responding? Powers the green
    light in the admin dashboard. Never proxies — checks locally."""
    client = await get_router_client()
    cfg = await get_router_config_store().get()
    reachable = await client.is_reachable()
    return {
        "reachable": reachable,
        "base_url": cfg.base_url,
        "source": cfg.source,
    }


# =====================================================================
# Overview — proxied
# =====================================================================


@router.get("/router/health")
async def router_health(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/health",
                           admin_user_id=admin)


@router.get("/router/upstreams")
async def router_upstreams(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/upstreams",
                           admin_user_id=admin)


# =====================================================================
# Mesh overlay — proxied
# =====================================================================


@router.get("/mesh/topology")
async def mesh_topology(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/mesh/topology",
                           admin_user_id=admin)


@router.get("/mesh/neighbours")
async def mesh_neighbours_list(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    """Helen-Router exposes neighbours through /mesh/topology, but a
    dedicated endpoint is convenient for paginated views. We forward
    to /mesh/topology and let the router return the slice — the
    upstream implementation is free to add a dedicated route later."""
    h = await _handler()
    return await h.forward(request, "/mesh/neighbours",
                           admin_user_id=admin)


@router.post("/mesh/reroute")
async def mesh_reroute(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    """Force a mesh recomputation. Body may include
    ``{"server_id": "...", "via": "router-id"}``."""
    h = await _handler()
    return await h.forward(
        request, "/mesh/reroute",
        admin_user_id=admin,
        audit_event="router.mesh.reroute",
    )


@router.delete("/mesh/neighbours/{neighbour_id}")
async def mesh_remove_neighbour(
    neighbour_id: str,
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, f"/mesh/neighbours/{neighbour_id}",
        admin_user_id=admin,
        audit_event="router.mesh.neighbour.remove",
    )


# =====================================================================
# Service registry — proxied
# =====================================================================


@router.get("/router/register")
async def registry_list(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/register",
                           admin_user_id=admin)


@router.post("/router/register")
async def registry_register(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/register",
        admin_user_id=admin,
        audit_event="router.registry.register",
    )


@router.put("/router/register/{server_id}")
async def registry_update(
    server_id: str,
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, f"/router/register/{server_id}",
        admin_user_id=admin,
        audit_event="router.registry.update",
    )


@router.delete("/router/register/{server_id}")
async def registry_delete(
    server_id: str,
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, f"/router/register/{server_id}",
        admin_user_id=admin,
        audit_event="router.registry.delete",
    )


# =====================================================================
# Reverse-proxy controls — proxied
# =====================================================================


@router.get("/router/proxy/log")
async def proxy_log(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    """Live access log. Streamed because the router may produce
    a very large response on a busy LAN."""
    h = await _handler()
    return await h.forward(request, "/router/proxy/log",
                           admin_user_id=admin, stream=True)


@router.get("/router/proxy/rate-limits")
async def proxy_rate_limits_get(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/proxy/rate-limits",
                           admin_user_id=admin)


@router.post("/router/proxy/rate-limits")
async def proxy_rate_limits_set(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/proxy/rate-limits",
        admin_user_id=admin,
        audit_event="router.proxy.rate_limits.update",
    )


@router.get("/router/proxy/ip-lists/{list_name}")
async def proxy_ip_list_get(
    list_name: str,
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, f"/router/proxy/ip-lists/{list_name}",
        admin_user_id=admin,
    )


@router.post("/router/proxy/ip-lists/{list_name}")
async def proxy_ip_list_set(
    list_name: str,
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, f"/router/proxy/ip-lists/{list_name}",
        admin_user_id=admin,
        audit_event="router.proxy.ip_list.update",
    )


# =====================================================================
# DNS — proxied
# =====================================================================


@router.get("/router/dns/records")
async def dns_records_list(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/dns/records",
                           admin_user_id=admin)


@router.post("/router/dns/records")
async def dns_records_create(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/dns/records",
        admin_user_id=admin,
        audit_event="router.dns.record.create",
    )


@router.delete("/router/dns/records")
async def dns_records_delete(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    """Delete by query params (?name=&type=) or body."""
    h = await _handler()
    return await h.forward(
        request, "/router/dns/records",
        admin_user_id=admin,
        audit_event="router.dns.record.delete",
    )


@router.get("/router/dns/blocklist")
async def dns_blocklist_get(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/dns/blocklist",
                           admin_user_id=admin)


@router.post("/router/dns/blocklist")
async def dns_blocklist_set(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/dns/blocklist",
        admin_user_id=admin,
        audit_event="router.dns.blocklist.update",
    )


@router.get("/router/dns/stats")
async def dns_stats(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/dns/stats",
                           admin_user_id=admin)


@router.get("/router/dns/log")
async def dns_log(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/dns/log",
                           admin_user_id=admin, stream=True)


@router.get("/router/dns/upstreams")
async def dns_upstreams_get(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/dns/upstreams",
                           admin_user_id=admin)


@router.post("/router/dns/upstreams")
async def dns_upstreams_set(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/dns/upstreams",
        admin_user_id=admin,
        audit_event="router.dns.upstreams.update",
    )


# =====================================================================
# NTP — proxied
# =====================================================================


@router.get("/router/ntp/status")
async def ntp_status(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/ntp/status",
                           admin_user_id=admin)


@router.post("/router/ntp/sync")
async def ntp_sync(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/ntp/sync",
        admin_user_id=admin,
        audit_event="router.ntp.sync",
    )


# =====================================================================
# UPnP — proxied
# =====================================================================


@router.get("/router/upnp/portmaps")
async def upnp_portmaps(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/upnp/portmaps",
                           admin_user_id=admin)


@router.post("/router/upnp/portmap")
async def upnp_portmap_create(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/upnp/portmap",
        admin_user_id=admin,
        audit_event="router.upnp.portmap.create",
    )


@router.delete("/router/upnp/portmap/{mapping_id}")
async def upnp_portmap_delete(
    mapping_id: str,
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, f"/router/upnp/portmap/{mapping_id}",
        admin_user_id=admin,
        audit_event="router.upnp.portmap.delete",
    )


@router.post("/router/upnp/discover")
async def upnp_discover(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/upnp/discover",
        admin_user_id=admin,
        audit_event="router.upnp.discover",
    )


# =====================================================================
# Vendor adapters — proxied
# =====================================================================


@router.post("/router/vendor/test")
async def vendor_test(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/vendor/test",
        admin_user_id=admin,
        audit_event="router.vendor.test",
    )


@router.post("/router/vendor/push")
async def vendor_push(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/vendor/push",
        admin_user_id=admin,
        audit_event="router.vendor.push",
    )


@router.get("/router/vendor/jobs")
async def vendor_jobs(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/vendor/jobs",
                           admin_user_id=admin)


# =====================================================================
# External routers — proxied
# =====================================================================


@router.get("/router/external")
async def external_routers_list(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/external",
                           admin_user_id=admin)


@router.post("/router/external/scan")
async def external_routers_scan(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/external/scan",
        admin_user_id=admin,
        audit_event="router.external.scan",
    )


# =====================================================================
# Connection broker — proxied
# =====================================================================


@router.get("/router/broker/status")
async def broker_status(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/broker/status",
                           admin_user_id=admin)


# =====================================================================
# Security — proxied
# =====================================================================


@router.get("/router/security")
async def security_overview(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/security",
                           admin_user_id=admin)


@router.post("/router/security/rotate-token")
async def security_rotate_token(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    """Token rotation is high-value — emits a critical-severity audit
    entry on both the attempt and outcome."""
    h = await _handler()
    return await h.forward(
        request, "/router/security/rotate-token",
        admin_user_id=admin,
        audit_event="router.security.token.rotate",
    )


@router.get("/router/security/subnets")
async def security_subnets_get(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/security/subnets",
                           admin_user_id=admin)


@router.post("/router/security/subnets")
async def security_subnets_set(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/security/subnets",
        admin_user_id=admin,
        audit_event="router.security.subnets.update",
    )


@router.post("/router/security/enforcement")
async def security_enforcement(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/security/enforcement",
        admin_user_id=admin,
        audit_event="router.security.enforcement.update",
    )


# =====================================================================
# Diagnostics — proxied
# =====================================================================


@router.post("/router/diag/ping")
async def diag_ping(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/diag/ping",
        admin_user_id=admin,
        audit_event="router.diag.ping",
    )


@router.post("/router/diag/traceroute")
async def diag_traceroute(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/diag/traceroute",
        admin_user_id=admin,
        audit_event="router.diag.traceroute",
    )


@router.post("/router/diag/dns")
async def diag_dns(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/diag/dns",
        admin_user_id=admin,
        audit_event="router.diag.dns",
    )


@router.post("/router/diag/portscan")
async def diag_portscan(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/diag/portscan",
        admin_user_id=admin,
        audit_event="router.diag.portscan",
    )


@router.post("/router/diag/bandwidth")
async def diag_bandwidth(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/diag/bandwidth",
        admin_user_id=admin,
        audit_event="router.diag.bandwidth",
    )


# =====================================================================
# Config + admin lifecycle — proxied
# =====================================================================


@router.get("/router/config")
async def config_get(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/config",
                           admin_user_id=admin)


@router.put("/router/config")
async def config_put(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/config",
        admin_user_id=admin,
        audit_event="router.config.update",
    )


@router.post("/router/config/validate")
async def config_validate(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/config/validate",
        admin_user_id=admin,
        audit_event="router.config.validate",
    )


@router.post("/router/admin/reload")
async def admin_reload(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/admin/reload",
        admin_user_id=admin,
        audit_event="router.admin.reload",
    )


@router.post("/router/admin/restart")
async def admin_restart(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/admin/restart",
        admin_user_id=admin,
        audit_event="router.admin.restart",
    )


# =====================================================================
# Routing rules — proxied
# =====================================================================


@router.get("/router/routing/rules")
async def routing_rules_list(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(request, "/router/routing/rules",
                           admin_user_id=admin)


@router.post("/router/routing/rules")
async def routing_rules_add(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    h = await _handler()
    return await h.forward(
        request, "/router/routing/rules",
        admin_user_id=admin,
        audit_event="router.routing.rule.add",
    )


@router.delete("/router/routing/rules")
async def routing_rules_delete(
    request: Request,
    admin: str = Depends(require_admin()),
) -> Response:
    """Body or query (?rule_id=...) selects which rule to remove."""
    h = await _handler()
    return await h.forward(
        request, "/router/routing/rules",
        admin_user_id=admin,
        audit_event="router.routing.rule.delete",
    )
