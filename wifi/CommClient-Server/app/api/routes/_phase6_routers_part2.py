"""Phase 6 part 2 router registration (AC + AD + AE)."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                                                    # pragma: no cover
    from fastapi import FastAPI


from app.api.routes import (
    admin_cluster,
    admin_observability,
    admin_security,
)


_PHASE6_PART2_MODULES = (
    admin_cluster,
    admin_observability,
    admin_security,
)


def register_phase6_part2_routers(app: "FastAPI") -> None:
    """Mount every Phase 6 part-2 router. Idempotent."""
    if getattr(app.state, "_phase6_part2_registered", False):
        return
    for m in _PHASE6_PART2_MODULES:
        r = getattr(m, "router", None)
        if r is None:
            continue
        app.include_router(r)
    app.state._phase6_part2_registered = True
