"""
app.domains.rbac — Role-Based Access Control (Phase 2 Module G).

Existing implementation locations:
    app.models.rbac                — Role, Permission, RoleAssignment ORM
    app.services.rbac.enforcer     — has_permission(), require_permission()
    app.services.rbac.registry     — Permission registry & metadata
    app.api.routes.admin_rbac      — /api/admin/rbac/* admin router
"""

from __future__ import annotations

from app.domains._safe_import import safe_import

_exports: dict = {}

# Routers
got = safe_import("app.api.routes.admin_rbac", ["router"])
if "router" in got:
    _exports["admin_rbac_router"] = got["router"]

# Services
_exports.update(safe_import(
    "app.services.rbac.enforcer",
    [
        "Enforcer",
        "has_permission",
        "require_permission",
        "check_permission",
        "PermissionDenied",
    ],
))
_exports.update(safe_import(
    "app.services.rbac.registry",
    [
        "PermissionRegistry",
        "register_permission",
        "list_permissions",
        "Permission",
    ],
))

# Models
_exports.update(safe_import(
    "app.models.rbac",
    [
        "Role",
        "Permission",
        "RolePermission",
        "RoleAssignment",
        "UserRole",
    ],
))

globals().update(_exports)
__all__ = sorted(_exports.keys())
