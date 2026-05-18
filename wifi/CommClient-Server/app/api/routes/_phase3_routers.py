"""Phase 3 router registration — wire-up helper.

Mounts every Phase 3 router (Modules M–Q) on the FastAPI ``app``. The
helper is idempotent — calling it twice is a no-op past the first time
thanks to the ``app.state._phase3_registered`` marker.

Each Phase-3 module declares its own ``/api/...`` prefix internally, so
we do NOT wrap them with an extra parent router (unlike Phase 2).

Usage
-----
In ``app/main.py`` after Phase 2 wire-up::

    from app.api.routes._phase3_routers import register_phase3_routers
    register_phase3_routers(app)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                                                    # pragma: no cover
    from fastapi import FastAPI


from app.api.routes import (
    workspaces,
    oauth,
    pairing_v2,
    admin_files,
    admin_updates,
)


_PHASE3_MODULES = (
    workspaces,
    oauth,
    pairing_v2,
    admin_files,
    admin_updates,
)


def register_phase3_routers(app: "FastAPI") -> None:
    """Mount every Phase 3 router. Idempotent."""
    if getattr(app.state, "_phase3_registered", False):
        return
    for module in _PHASE3_MODULES:
        r = getattr(module, "router", None)
        if r is None:
            continue
        app.include_router(r)
    app.state._phase3_registered = True
