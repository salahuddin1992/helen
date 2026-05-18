"""Phase 6 part 1 router registration (AA + AB + AF).

Mounts every Phase 6 router contributed by Modules AA (Disaster Recovery),
AB (Compliance & Privacy Pack), and AF (Webhooks v2) onto the FastAPI
``app``. The helper is idempotent.

Usage
-----
In ``app/main.py`` after ``register_phase5_routers``::

    from app.api.routes._phase6_routers_part1 import register_phase6_part1_routers
    register_phase6_part1_routers(app)

Background workers (DR drill scheduler, retention scheduler, webhook
delivery engine) are NOT started here — call their respective ``start()``
helpers from the FastAPI lifespan. See the integration notes shipped with
the module.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                                                    # pragma: no cover
    from fastapi import FastAPI


from app.api.routes import (
    admin_dr,
    compliance,
    admin_compliance,
    webhooks_v2,
    admin_webhooks,
)
# NOTE: ``admin_dr_v2`` is registered via ``app.api.routes.__init__`` on
# the aggregator ``api_router`` (which adds the ``/api`` prefix) — NOT
# from this helper, because its prefix is ``/admin/dr`` (relative to
# ``/api``) while the legacy ``admin_dr`` carries the full
# ``/api/admin/dr`` prefix.  Mixing the two in the same helper would
# double-prefix one or under-prefix the other.


_PHASE6_PART1_MODULES = (
    admin_dr,
    compliance,
    admin_compliance,
    webhooks_v2,
    admin_webhooks,
)
