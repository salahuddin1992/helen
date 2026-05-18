"""TCP simultaneous-open coordination (best-effort).

TCP hole-punching is finicky compared to UDP — it depends on
SYN-vs-SYN simultaneous-open behaviour which not every NAT honours.
We attempt it as a fallback when UDP punch fails and the peer
doesn't have a reverse tunnel.

Algorithm:
  1. Both peers learn the other's public (ip, port) via rendezvous.
  2. Each peer initiates ``connect()`` to the other simultaneously.
  3. If both NATs allow the SYN cross-over, the connection
     establishes; otherwise both connects fail.

This module returns the established socket (or None).
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


async def punch(peer_id: str, *,
                local_port: int = 0) -> Optional[socket.socket]:
    """Attempt TCP simultaneous-open to peer_id.

    Returns the opened socket on success, None on failure.
    Raises HolePunchError if disabled or no endpoint.
    """
    cfg = get_config()
    if not cfg.enable_tcp_punch:
        raise HolePunchError("tcp punch disabled by config")
    endpoint = await resolve_peer_endpoint(peer_id)
    if endpoint is None:
        raise HolePunchError(f"no endpoint for {peer_id}")
    host, port = endpoint

    loop = asyncio.get_event_loop()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)  # POSIX only
    except (AttributeError, OSError):
        pass
    s.setblocking(False)
    try:
        s.bind(("0.0.0.0", local_port))
    except OSError as e:
        s.close()
        raise HolePunchError(f"bind failed: {e}")

    success_sock: Optional[socket.socket] = None
    for _ in range(cfg.punch_attempts):
        try:
            await asyncio.wait_for(
                loop.sock_connect(s, (host, port)),
                timeout=cfg.punch_timeout_sec,
            )
            success_sock = s
            break
        except (ConnectionRefusedError, asyncio.TimeoutError):
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug("tcp_punch_attempt_failed", error=str(e)[:80])
            await asyncio.sleep(0.2)

    ok = success_sock is not None
    emit("nat.tcp_punch", {
        "peer_id": peer_id, "ok": ok, "endpoint": f"{host}:{port}",
    })
    if not ok:
        try:
            s.close()
        except Exception:
            pass
    return success_sock


def snapshot() -> dict:
    cfg = get_config()
    return {
        "enabled":      cfg.enable_tcp_punch,
        "attempts":     cfg.punch_attempts,
        "timeout_sec":  cfg.punch_timeout_sec,
    }
