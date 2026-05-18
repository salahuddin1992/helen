"""
Online-Mode toggle endpoints.

Three endpoints, three permission tiers
---------------------------------------
* ``GET  /api/online-mode/status`` — *public* (any authenticated
  user). Lets the client UI render an indicator showing whether the
  deployment is currently in pure-LAN mode or extended-online mode.

* ``POST /api/admin/online-mode/enable`` — admin-only. Flips the
  master switch on, starts every registered online-capable service.

* ``POST /api/admin/online-mode/disable`` — admin-only. Flips the
  master switch off, stops every registered online-capable service.

The client deliberately can NOT flip the switch — only an admin
can. The client's UI button (in the desktop renderer) calls these
endpoints and surfaces the result; non-admin users see the
indicator as read-only.

Why split the prefix
--------------------
We want the read endpoint to be reachable from the unprivileged
client (so the UI can show "online mode: off" everywhere), but we
do not want the write endpoints to be confused for a regular user
operation. Putting the writes under ``/api/admin/...`` mirrors the
rest of Helen's RBAC convention and keeps the admin RBAC layer
honest.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.deps import get_current_user_id
from app.core.security_utils import require_role


# Public read router — mounted under /api
public_router = APIRouter(prefix="/online-mode", tags=["online-mode"])

# Admin write router — mounted under /api/admin
admin_router = APIRouter(prefix="/admin/online-mode",
                          tags=["admin", "online-mode"])


class _ToggleBody(BaseModel):
    reason: str | None = None


@public_router.get("/status")
async def online_mode_status(
    user_id: str = Depends(get_current_user_id),
):
    """Read-only status endpoint. Any logged-in user can call this so
    the client UI can render the indicator. Returns the current
    on/off state plus a redacted view of registered services (so the
    UI can show *which* online features are actually wired up)."""
    from app.services.online_mode_gate import get_online_mode_gate
    gate = get_online_mode_gate()
    if gate is None:
        return {
            "configured": False,
            "enabled": False,
            "services": [],
        }
    full = gate.status()
    # Strip history + actor names from the public view; users don't
    # need to see who flipped the switch.
    return {
        "configured": True,
        "enabled": full["enabled"],
        "last_change_at": full["last_change_at"],
        "services": [
            {"name": s["name"], "running": s["running"]}
            for s in full["services"]
        ],
    }


@admin_router.post("/enable")
async def online_mode_enable(
    body: _ToggleBody | None = None,
    actor_id: str = Depends(require_role("admin")),
):
    from app.services.online_mode_gate import get_online_mode_gate
    gate = get_online_mode_gate()
    if gate is None:
        return {"configured": False, "enabled": False}
    return await gate.enable(
        actor=actor_id,
        reason=(body.reason if body else None),
    )


@admin_router.post("/disable")
async def online_mode_disable(
    body: _ToggleBody | None = None,
    actor_id: str = Depends(require_role("admin")),
):
    from app.services.online_mode_gate import get_online_mode_gate
    gate = get_online_mode_gate()
    if gate is None:
        return {"configured": False, "enabled": False}
    return await gate.disable(
        actor=actor_id,
        reason=(body.reason if body else None),
    )


@admin_router.get("/full-status")
async def online_mode_full_status(
    actor_id: str = Depends(require_role("admin")),
):
    """Admin-only view that includes flip history + actor info."""
    from app.services.online_mode_gate import get_online_mode_gate
    gate = get_online_mode_gate()
    if gate is None:
        return {"configured": False}
    return {"configured": True, **gate.status()}


__all__ = ["public_router", "admin_router"]
