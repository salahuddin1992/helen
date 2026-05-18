"""NAT-type taxonomy + traversal capability matrix.

Five canonical NAT types (RFC 3489 / RFC 4787 hybrid):

    OPEN              — no NAT, public IP
    FULL_CONE         — same external port for all destinations
    RESTRICTED        — same port, but only inbound from contacted hosts
    PORT_RESTRICTED   — same port, but only from contacted (host, port)
    SYMMETRIC         — fresh external port per destination (worst case)
    UNKNOWN           — detection failed / not yet probed
"""

from __future__ import annotations

from enum import Enum


class NATType(str, Enum):
    OPEN              = "open"
    FULL_CONE         = "full_cone"
    RESTRICTED        = "restricted"
    PORT_RESTRICTED   = "port_restricted"
    SYMMETRIC         = "symmetric"
    UNKNOWN           = "unknown"


# Traversal-strategy compatibility matrix.
# Rows = our NAT, columns = peer NAT, value = best strategy name.
_HOLE_PUNCH_OK = {
    (NATType.OPEN,            NATType.OPEN),
    (NATType.OPEN,            NATType.FULL_CONE),
    (NATType.OPEN,            NATType.RESTRICTED),
    (NATType.OPEN,            NATType.PORT_RESTRICTED),
    (NATType.FULL_CONE,       NATType.OPEN),
    (NATType.FULL_CONE,       NATType.FULL_CONE),
    (NATType.FULL_CONE,       NATType.RESTRICTED),
    (NATType.FULL_CONE,       NATType.PORT_RESTRICTED),
    (NATType.RESTRICTED,      NATType.OPEN),
    (NATType.RESTRICTED,      NATType.FULL_CONE),
    (NATType.RESTRICTED,      NATType.RESTRICTED),
    (NATType.RESTRICTED,      NATType.PORT_RESTRICTED),
    (NATType.PORT_RESTRICTED, NATType.OPEN),
    (NATType.PORT_RESTRICTED, NATType.FULL_CONE),
    (NATType.PORT_RESTRICTED, NATType.RESTRICTED),
    # PORT_RESTRICTED ↔ PORT_RESTRICTED is racey but usually works.
    (NATType.PORT_RESTRICTED, NATType.PORT_RESTRICTED),
}


def hole_punch_compatible(local: NATType, remote: NATType) -> bool:
    """Returns True iff UDP hole-punch is expected to succeed."""
    if local is NATType.UNKNOWN or remote is NATType.UNKNOWN:
        return False
    return (local, remote) in _HOLE_PUNCH_OK


def best_strategy(local: NATType, remote: NATType) -> str:
    """Returns the recommended strategy name for the (local, remote)
    pair: 'direct' / 'hole_punch' / 'reverse_tunnel' / 'relay'.
    """
    if local is NATType.OPEN and remote is NATType.OPEN:
        return "direct"
    if hole_punch_compatible(local, remote):
        return "hole_punch"
    if local is NATType.SYMMETRIC or remote is NATType.SYMMETRIC:
        # Symmetric NAT defeats hole-punch — fall back.
        return "reverse_tunnel"
    return "relay"


def description(t: NATType) -> str:
    return {
        NATType.OPEN:            "no NAT, public IP",
        NATType.FULL_CONE:       "same external port to all peers",
        NATType.RESTRICTED:      "filtered by source IP only",
        NATType.PORT_RESTRICTED: "filtered by source IP+port",
        NATType.SYMMETRIC:       "unique external port per destination",
        NATType.UNKNOWN:         "not detected",
    }.get(t, "unknown")
