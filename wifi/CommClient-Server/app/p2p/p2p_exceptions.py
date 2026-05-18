"""Custom exception hierarchy for the p2p package."""

from __future__ import annotations


class P2PError(Exception):
    """Base class for every p2p exception."""


class PeerNotFoundError(P2PError):
    """Referenced peer_id has no entry in the registry."""


class PeerHandshakeError(P2PError):
    """Mutual-authentication handshake failed."""


class PeerSelectionError(P2PError):
    """No peer satisfied the selection criteria."""


class PeerConnectionError(P2PError):
    """Outbound connection to a peer failed."""


class PeerForwardingError(P2PError):
    """Multi-hop message forward could not complete."""


class PeerNATTraversalError(P2PError):
    """All NAT-traversal strategies failed for the target."""


class PeerQuarantinedError(P2PError):
    """The selected peer is currently quarantined."""


class PeerSessionError(P2PError):
    """Session-level error (expired token, bad sequence, etc.)."""
