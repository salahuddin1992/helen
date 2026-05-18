"""
app.domains.agents — Programmatic agents / bots (Phase 3 Module L).

Existing implementation locations:
    app.api.routes.agents        — /api/agents/* router
    app.services.agent_manager   — lifecycle
    app.services.agent_dispatcher — event fan-out
    app.models.agent             — Agent ORM
"""

from __future__ import annotations

from app.domains._safe_import import safe_import

_exports: dict = {}

got = safe_import("app.api.routes.agents", ["router"])
if "router" in got:
    _exports["agents_router"] = got["router"]

_exports.update(safe_import(
    "app.services.agent_manager",
    ["AgentManager", "register_agent", "deregister_agent", "list_agents"],
))
_exports.update(safe_import(
    "app.services.agent_dispatcher",
    ["AgentDispatcher", "dispatch_event"],
))

_exports.update(safe_import(
    "app.models.agent",
    ["Agent", "AgentType", "AgentStatus"],
))

globals().update(_exports)
__all__ = sorted(_exports.keys())
