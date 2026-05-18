"""Phase 7 part 1 router registration (AG + AH + AI).

Mounts every Phase 7 router contributed by Modules AG (Billing & Usage
Metering), AH (Marketplace & Plugin System), and AI (Advanced Analytics
& BI) onto the FastAPI ``app``. The helper is idempotent.

Usage
-----
In ``app/main.py`` after ``register_phase6_part1_routers``::

    from app.api.routes._phase7_routers_part1 import register_phase7_part1_routers
    register_phase7_part1_routers(app)

Background workers (metering flusher, analytics ingester, dunning
scheduler) are NOT started here — call their respective ``start``
helpers from the FastAPI lifespan:

    from app.services.billing.metering import start_background_flusher
    from app.services.analytics.event_ingester import start_background_ingester

    start_background_flusher()
    start_background_ingester()
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                                                       # pragma: no cover
    from fastapi import FastAPI


from app.api.routes import (
    admin_analytics,
    admin_billing,
    admin_plugins,
    analytics,
    billing,
    plugins,
)


_PHASE7_PART1_MODULES = (
    billing,
    admin_billing,
    plugins,
    admin_plugins,
    analytics,
    admin_analytics,
)


def register_phase7_part1_routers(app: "FastAPI") -> None:
    """Mount every Phase 7 part-1 router. Idempotent."""
    if getattr(app.state, "_phase7_part1_registered", False):
        return
    for module in _PHASE7_PART1_MODULES:
        r = getattr(module, "router", None)
        if r is None:
            continue
        app.include_router(r)
    app.state._phase7_part1_registered = True
