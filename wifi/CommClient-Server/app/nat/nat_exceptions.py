"""Custom exception hierarchy for the NAT traversal package."""

from __future__ import annotations


class NATError(Exception):
    """Base class for every NAT-traversal exception."""


class NATDetectionError(NATError):
    """STUN-based NAT-type detection failed."""


class NATNotTraversableError(NATError):
    """No strategy succeeded — peer is unreachable from here."""


class STUNError(NATError):
    """STUN-protocol error (timeout, malformed response)."""


class RendezvousError(NATError):
    """Rendezvous coordination failed."""


class HolePunchError(NATError):
    """UDP/TCP hole-punch attempt failed."""


class ReverseTunnelError(NATError):
    """Outbound reverse tunnel could not be established."""


class RelayFallbackError(NATError):
    """The last-resort relay path also failed."""


class NATSessionError(NATError):
    """Session lifecycle error (duplicate id, expired)."""
