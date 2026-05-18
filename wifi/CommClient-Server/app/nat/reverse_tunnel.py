"""Reverse-tunnel client — outbound WebSocket to Helen-Rendezvous.

When this peer is behind NAT, we open a long-lived outbound WS
connection to Helen-Rendezvous. External clients can then hit
``rendezvous_host/peer/<our_id>/...`` and the request is proxied
back through the tunnel.

Wraps ``services.connectivity.reverse_tunnel.ReverseTunnelClient``
so the NAT package doesn't import the lower-level connectivity
module directly.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.core.logging import get_logger
from app.nat.nat_config import get_config
from app.nat.nat_events import emit
from app.nat.nat_exceptions import ReverseTunnelError

logger = get_logger(__name__)


_client = None


async def start() -> bool:
    """Bring up the reverse tunnel. Idempotent."""
    global _client
    cfg = get_config()
    if not cfg.enable_reverse_tunnel:
        return False
    if not cfg.rendezvous_host:
        return False
    try:
        from app.services.connectivity.reverse_tunnel import (
            ReverseTunnelClient,
        )
    except ImportError as e:
        raise ReverseTunnelError(f"reverse_tunnel primitive missing: {e}")
    if _client is None:
        _client = ReverseTunnelClient()
    try:
        await _client.start()
        emit("nat.tunnel_up", {"rendezvous_host": cfg.rendezvous_host})
        return True
    except Exception as e:
        logger.warning("reverse_tunnel_start_failed", error=str(e))
        emit("nat.tunnel_failed", {"error": str(e)[:80]})
        return False


async def stop() -> None:
    global _client
    if _client is None:
        return
    try:
        # ReverseTunnelClient may expose ``stop`` or ``close``.
        if hasattr(_client, "stop"):
            await _client.stop()
        elif hasattr(_client, "close"):
            await _client.close()
    except Exception as e:
        logger.warning("reverse_tunnel_stop_failed", error=str(e))
    finally:
        _client = None
        emit("nat.tunnel_down", {})


def is_running() -> bool:
    return _client is not None


def snapshot() -> dict:
    cfg = get_config()
    return {
        "enabled":          cfg.enable_reverse_tunnel,
        "rendezvous_host":  cfg.rendezvous_host,
        "running":          is_running(),
    }
