"""
app.domains.tenancy — Multi-tenancy / workspaces (Phase 3 Module M).

Existing implementation locations:
    app.api.routes.workspaces           — /api/workspaces/* router
    app.services.tenancy.workspace_service
    app.models.workspace                — Workspace, WorkspaceMember ORM
"""

from __future__ import annotations

from app.domains._safe_import import safe_import

_exports: dict = {}

got = safe_import("app.api.routes.workspaces", ["router"])
if "router" in got:
    _exports["workspaces_router"] = got["router"]

_exports.update(safe_import(
    "app.services.tenancy.workspace_service",
    [
        "WorkspaceService",
        "create_workspace",
        "list_workspaces_for_user",
        "add_member",
        "remove_member",
        "update_workspace",
        "delete_workspace",
    ],
))

_exports.update(safe_import(
    "app.models.workspace",
    ["Workspace", "WorkspaceMember", "WorkspaceRole"],
))

globals().update(_exports)
__all__ = sorted(_exports.keys())
