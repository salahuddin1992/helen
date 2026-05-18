"""Phase 7 part 2 router registration (AJ + AK + AL).

Wires the federation v2 mesh, edge computing, and zero-trust router
modules into the FastAPI application. Idempotent — repeated calls are
no-ops.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                                                    # pragma: no cover
    from fastapi import FastAPI


from app.api.routes import (
    admin_edge,
    admin_federation_v2,
    admin_zt,
    edge_public,
    federation_v2_public,
    zt_client,
)


_PHASE7_PART2_MODULES = (
    federation_v2_public,
    admin_federation_v2,
    admin_edge,
    edge_public,
    admin_zt,
    zt_client,
)


def register_phase7_part2_routers(app: "FastAPI") -> None:
    """Mount every Phase 7 part-2 router. Idempotent."""
    if getattr(app.state, "_phase7_part2_registered", False):
        return
    for m in _PHASE7_PART2_MODULES:
        r = getattr(m, "router", None)
        if r is None:
            continue
        app.include_router(r)
    app.state._phase7_part2_registered = True
