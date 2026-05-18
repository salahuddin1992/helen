"""
LAN transport health — single endpoint surface for the TransportCoordinator.

Exposes the per-transport state snapshot tracked by
`app.services.transport_coordinator`:

    GET /api/transports/health

Returned shape:
    {
      "summary":  {alive, total_enabled, healthy},
      "transports": {
          "websocket":     {name, enabled, running, port, last_ok_at, clients, error, extra},
          "udp_broadcast": {...},
          "mdns":          {...},
          "tcp_fallback":  {...},
      },
      "listening_ports": [3000, 41234, 41235, 5353],
    }

This endpoint is authenticated (any logged-in user) so the admin UI
panel and client-side fallback probes can both read it. No write path —
state is entirely managed by the coordinator.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.deps import get_current_user_id
from app.services.transport_coordinator import transport_coordinator

router = APIRouter(prefix="/transports", tags=["transports"])


@router.get("/health")
async def transports_health(user_id: str = Depends(get_current_user_id)) -> dict:
    return transport_coordinator.get_snapshot()


__all__ = ["router"]
