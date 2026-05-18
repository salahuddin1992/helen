"""
Helen-Router Control — server-side proxy layer.

This package implements the Helen-Server side of the Helen-Router
admin protocol. The router itself (a separate FastAPI process on
``:8080``) exposes its own ``/router/*`` and ``/mesh/*`` admin API.
Operators authenticate to *Helen-Server* (with the existing JWT +
RBAC layer) and the requests are forwarded — authentication
swapped to the shared router token — to Helen-Router.

Why a proxy and not direct calls?
---------------------------------
  * **Single auth surface.** Admins already log into Helen-Server.
    Forcing them to mint a second token for the router would
    double the credential-management surface area.
  * **Audit.** Every write op flows through Helen-Server's audit
    pipeline (``app.core.audit.audit_log``) so the immutable
    chain captures router config changes as well as application
    config changes.
  * **Network isolation.** Helen-Router often lives on an
    operator-only management VLAN. Putting Helen-Server in
    front lets the admin UI reach it without exposing the
    router's port to the wider client LAN.

Public surface
--------------
  * :class:`HelenRouterClient`  — async httpx client, connection
    pooling, retry, token injection.
  * :class:`RouterProxyHandler` — generic request forwarder with
    streaming, header rewriting and latency logging.
  * :class:`RouterAuditHook`    — emits ``audit_log`` entries
    for every write op (POST/PUT/DELETE).
  * :class:`RouterConfigStore`  — settings + DB-backed override
    for the router base URL and token.
  * :class:`RouterResponse`     — typed wrapper for the result of
    a forwarded call (status, headers, body, error).

The matching APIRouter lives in
``app/api/routes/admin_router_control.py``.
"""

from __future__ import annotations

from app.services.router_control.audit_hook import RouterAuditHook
from app.services.router_control.config_store import (
    RouterConfigStore,
    get_router_config_store,
)
from app.services.router_control.proxy_handler import RouterProxyHandler
from app.services.router_control.router_client import (
    HelenRouterClient,
    RouterResponse,
    RouterUnreachableError,
    get_router_client,
)

__all__ = [
    "HelenRouterClient",
    "RouterResponse",
    "RouterUnreachableError",
    "RouterProxyHandler",
    "RouterAuditHook",
    "RouterConfigStore",
    "get_router_client",
    "get_router_config_store",
]
