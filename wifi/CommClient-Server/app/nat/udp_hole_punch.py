"""UDP hole-punching coordinator.

Algorithm:

  1. Both peers learn each other's public (ip, port) via rendezvous.
  2. Both peers send N small UDP packets to the other's public
     endpoint at the same instant.
  3. The first packet to arrive opens the NAT mapping in each
     direction (the symmetry creates the punch).
  4. If a reply is received within ``punch_timeout_sec``, the punch
     succeeded.

This module returns success/failure; the actual data path uses the
opened socket via the upper layer (services.connectivity.hole_punch
in the existing project).
"""

from __future__ import annotations

import asyncio
import socket
from typing import Optional

from app.core.logging import get_logger
from app.nat.nat_config import get_config
from app.nat.nat_events import emit
from app.nat.nat_exceptions import HolePunchError
from app.nat.rendezvous_client import resolve_peer_endpoint

logger = get_logger(__name__)


async def punch(peer_id: str, *, local_port: int = 0) -> bool:
    """Attempt UDP hole-punch to ``peer_id``.

    Returns True iff we received at least one reply within timeout.
    Raises HolePunchError on configuration / lookup failure.
    """
    cfg = get_config()
    if not cfg.enable_udp_punch:
        raise HolePunchError("udp punch disabled by config")

    endpoint = await resolve_peer_endpoint(peer_id)
    if endpoint is None:
        # Try the existing connectivity helper as a fallback.
        try:
            from app.services.connectivity.hole_punch import HolePunchClient
            client = HolePunchClient()
            ok = await client.try_punch(peer_id)
            emit("nat.udp_punch", {"peer_id": peer_id, "ok": ok,
                                    "via": "service_helper"})
            return ok
        except Exception:
            raise HolePunchError(f"no endpoint for {peer_id}")

    host, port = endpoint
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", local_port))
    loop = asyncio.get_event_loop()
    payload = b"HELEN_PUNCH:" + peer_id.encode()[:48]
    success = False
    try:
        # Burst N packets to overcome any drop on the open mapping.
        for _ in range(cfg.punch_packet_count):
            try:
                await loop.sock_sendto(sock, payload, (host, port))  # type: ignore[attr-defined]
            except AttributeError:
                # Python < 3.11 fallback.
                sock.sendto(payload, (host, port))
            except Exception as e:
                logger.debug("punch_send_failed", error=str(e)[:80])
        try:
            data = await asyncio.wait_for(
                loop.sock_recv(sock, 1024),
                timeout=cfg.punch_timeout_sec,
            )
            if data:
                success = True
        except asyncio.TimeoutError:
            success = False
    finally:
        try:
            sock.close()
        except Exception:
            pass

    emit("nat.udp_punch", {
        "peer_id": peer_id, "ok": success,
        "endpoint": f"{host}:{port}",
    })
    return success


def snapshot() -> dict:
    cfg = get_config()
    return {
        "enabled":          cfg.enable_udp_punch,
        "packet_count":     cfg.punch_packet_count,
        "timeout_sec":      cfg.punch_timeout_sec,
        "attempts":         cfg.punch_attempts,
    }
