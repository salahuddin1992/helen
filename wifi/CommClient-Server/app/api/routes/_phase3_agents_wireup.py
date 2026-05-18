"""
Module L — Helen Agent router wire-up.

Idempotent helper that mounts the agents router under ``/api`` without
touching ``app/api/routes/__init__.py``. Call it once during application
startup, e.g.::

    from app.api.routes._phase3_agents_wireup import register_agents_router
    register_agents_router(app)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI


from app.api.routes import agents as agents_module


def register_agents_router(app: "FastAPI") -> None:
    """Mount the agents router under the ``/api`` prefix. Idempotent."""
    if getattr(app.state, "_phase3_agents_registered", False):
        return
    parent = APIRouter(prefix="/api")
    parent.include_router(agents_module.router)
    app.include_router(parent)

    # Kick off the background stale-reaper task.
    try:
        from app.services.agents.manager import get_agent_manager
        get_agent_manager().start_background()
    except Exception:  # pragma: no cover
        pass

    app.state._phase3_agents_registered = True


__all__ = ["register_agents_router"]
