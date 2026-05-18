"""Phase 5 router registration — wire-up helper.

Mounts every Phase 5 router (Modules Y, Z) on the FastAPI ``app``. Module X
(Helen-CLI) is a separate binary and contributes no server routes.

The helper is idempotent.

Usage
-----
In ``app/main.py`` after ``register_phase3_routers``::

    from app.api.routes._phase5_routers import register_phase5_routers
    register_phase5_routers(app)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                                                    # pragma: no cover
    from fastapi import FastAPI


from app.api.routes import (
    admin_bridges,
    ai_assistant,
    admin_ai,
)


_PHASE5_MODULES = (
    admin_bridges,
    ai_assistant,
    admin_ai,
)


def register_phase5_routers(app: "FastAPI") -> None:
    """Mount every Phase 5 router. Idempotent."""
    if getattr(app.state, "_phase5_registered", False):
        return
    for module in _PHASE5_MODULES:
        r = getattr(module, "router", None)
        if r is None:
            continue
        app.include_router(r)
    app.state._phase5_registered = True
