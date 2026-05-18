"""Peer-to-Peer networking package.

This package consolidates peer-level concerns (identity, handshake,
gossip, DHT, selection, relay, NAT traversal) into one organised
namespace while delegating the heavy lifting to existing services.

Public entry points:

    from app.p2p import (
        get_p2p_manager, start_p2p, stop_p2p,
    )
"""

from app.p2p.p2p_manager import (                                # noqa: F401
    get_p2p_manager,
    start_p2p,
    stop_p2p,
)
