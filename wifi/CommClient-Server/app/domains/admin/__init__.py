"""
app.domains.admin — Admin-only routers (Phase 2 + 3 consolidated).

Existing implementation locations:
    app.api.routes.admin           — main /api/admin/* aggregator
    app.api.routes.admin_audit     — audit-log viewer
    app.api.routes.admin_config    — config inspection / hot-reload
    app.api.routes.admin_files     — file admin
    app.api.routes.admin_health    — health probes
    app.api.routes.admin_logs      — log streaming
    app.api.routes.admin_metrics   — Prometheus exposition (admin)
    app.api.routes.admin_peers     — peer acceptance
    app.api.routes.admin_rbac      — role/permission CRUD
    app.api.routes.admin_tls       — cert mgmt
    app.api.routes.admin_updates   — auto-update admin
    app.api.routes.secret_admin    — master-code gated panel
"""

from __future__ import annotations

from app.domains._safe_import import safe_import

_exports: dict = {}


def _add(modpath: str, alias: str) -> None:
    got = safe_import(modpath, ["router"])
    if "router" in got:
        _exports[alias] = got["router"]


_add("app.api.routes.admin",         "admin_router")
_add("app.api.routes.admin_audit",   "admin_audit_router")
_add("app.api.routes.admin_config",  "admin_config_router")
_add("app.api.routes.admin_files",   "admin_files_router")
_add("app.api.routes.admin_health",  "admin_health_router")
_add("app.api.routes.admin_logs",    "admin_logs_router")
_add("app.api.routes.admin_metrics", "admin_metrics_router")
_add("app.api.routes.admin_peers",   "admin_peers_router")
_add("app.api.routes.admin_rbac",    "admin_rbac_router")
_add("app.api.routes.admin_tls",     "admin_tls_router")
_add("app.api.routes.admin_updates", "admin_updates_router")
_add("app.api.routes.secret_admin",  "secret_admin_router")

globals().update(_exports)
__all__ = sorted(_exports.keys())
