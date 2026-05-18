"""NAT-traversal facade — hole-punch + reverse tunnel + relay fallback."""

from __future__ import annotations

import os

from app.p2p.p2p_config import get_config
from app.p2p.p2p_exceptions import PeerNATTraversalError


def rendezvous_available() -> bool:
    return bool(os.environ.get("HELEN_RENDEZVOUS_HOST"))


def supported_strategies() -> list[str]:
    cfg = get_config()
    out: list[str] = []
    if cfg.enable_hole_punch:
        out.append("hole_punch")
    if cfg.enable_reverse_tunnel and rendezvous_available():
        out.append("reverse_tunnel")
    out.append("relay")
    return out


async def attempt_hole_punch(peer_id: str) -> bool:
    """Try a UDP hole punch. Returns True if successful."""
    try:
        from app.services.connectivity.hole_punch import HolePunchClient
        client = HolePunchClient()
        return await client.try_punch(peer_id)
    except Exception:
        return False


async def open_reverse_tunnel() -> bool:
    """Ensure our outbound rendezvous tunnel is up."""
    try:
        from app.services.connectivity.reverse_tunnel import ReverseTunnelClient
        client = ReverseTunnelClient()
        await client.start()
        return True
    except Exception:
        return False


async def traverse(peer_id: str) -> str:
    """Run the strategy ladder; return the name of the first one
    that succeeds. Raises PeerNATTraversalError on full failure."""
    for strategy in supported_strategies():
        if strategy == "hole_punch":
            if await attempt_hole_punch(peer_id):
                return strategy
        elif strategy == "reverse_tunnel":
            if await open_reverse_tunnel():
                return strategy
        elif strategy == "relay":
            # Always available — caller falls back to relay forwarding.
            return strategy
    raise PeerNATTraversalError(f"no strategy worked for {peer_id!r}")


def nat_snapshot() -> dict:
    return {
        "rendezvous_available": rendezvous_available(),
        "strategies":           supported_strategies(),
    }
