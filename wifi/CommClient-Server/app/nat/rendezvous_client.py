"""Rendezvous client — coordinator for hole-punch + tunnel discovery.

Helen-Rendezvous (the optional helper service) does three things:

  * Tells two peers each other's *public* (ip, port) so they can
    hole-punch.
  * Holds an outbound reverse tunnel from peers behind NAT, so an
    external client can ``HELEN_RENDEZVOUS_HOST/peer/<id>`` and have
    the request proxied back.
  * Forwards bytes blindly (TCP relay) as a last resort.

This module is the *client side*. The server side lives outside the
Helen-Server process (separate exe).
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.core.logging import get_logger
from app.nat.nat_config import get_config
from app.nat.nat_events import emit
from app.nat.nat_exceptions import RendezvousError

logger = get_logger(__name__)


def is_configured() -> bool:
    return bool(get_config().rendezvous_host)


async def resolve_peer_endpoint(peer_id: str) -> Optional[tuple[str, int]]:
    """Ask the rendezvous service for the peer's public (ip, port)
    needed for hole-punch coordination."""
    cfg = get_config()
    if not is_configured():
        return None
    try:
        import httpx
    except ImportError:
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(
                f"http://{cfg.rendezvous_host}:{cfg.rendezvous_port}"
                f"/peer/{peer_id}/endpoint"
            )
        if r.status_code != 200:
            return None
        d = r.json() or {}
        host = str(d.get("host") or "")
        port = int(d.get("port") or 0)
        if host and port:
            emit("rendezvous.endpoint", {
                "peer_id": peer_id, "host": host, "port": port,
            })
            return host, port
    except Exception as e:
        logger.debug("rendezvous_resolve_failed",
                     peer_id=peer_id[:24], error=str(e)[:80])
    return None


async def announce_self(peer_id: str, public_host: str,
                         public_port: int) -> bool:
    """Tell rendezvous our public binding so others can find us."""
    cfg = get_config()
    if not is_configured():
        return False
    try:
        import httpx
    except ImportError:
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.post(
                f"http://{cfg.rendezvous_host}:{cfg.rendezvous_port}"
                f"/peer/{peer_id}/announce",
                json={"host": public_host, "port": public_port},
            )
        return r.status_code == 200
    except Exception as e:
        logger.debug("rendezvous_announce_failed", error=str(e)[:80])
        return False


def snapshot() -> dict:
    cfg = get_config()
    return {
        "configured":       is_configured(),
        "rendezvous_host":  cfg.rendezvous_host,
        "rendezvous_port":  cfg.rendezvous_port,
    }
