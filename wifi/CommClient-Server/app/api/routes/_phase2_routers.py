"""
Phase 2 router registration — wire-up helper.

Single call to mount every Phase-2 admin router on the existing FastAPI
``app``. We deliberately avoid modifying ``app/api/routes/__init__.py``;
this helper is imported lazily wherever the application's startup wants it.

Usage
-----
In ``app/main.py`` (or anywhere after the ``api_router`` is mounted)::

    from app.api.routes._phase2_routers import register_phase2_routers
    register_phase2_routers(app)

The routers are mounted with the ``/api`` prefix so that their declared
``/admin/...`` paths line up with the rest of the API surface.

The helper is idempotent — calling it twice raises no error; FastAPI will
just refuse to register the same operation_id twice, so we guard against
that with a marker on ``app.state``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

if TYPE_CHECKING:                                                    # pragma: no cover
    from fastapi import FastAPI


# Import each Phase-2 router. Each module exposes ``router``.
from app.api.routes import (
    admin_audit,
    admin_config,
    admin_health,
    admin_logs,
    admin_metrics,
    admin_rbac,
    admin_tls,
)

_PHASE2_MODULES = (
    admin_logs,
    admin_metrics,
    admin_rbac,
    admin_audit,
    admin_config,
    admin_tls,
    admin_health,
)


def register_phase2_routers(app: "FastAPI") -> None:
    """Mount every Phase 2 admin router under ``/api``. Idempotent."""
    if getattr(app.state, "_phase2_registered", False):
        return

    parent = APIRouter(prefix="/api")
    for module in _PHASE2_MODULES:
        r = getattr(module, "router", None)
        if r is None:
            continue
        parent.include_router(r)
    app.include_router(parent)

    app.state._phase2_registered = True
