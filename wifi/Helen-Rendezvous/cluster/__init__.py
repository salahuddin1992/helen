"""
Helen-Rendezvous — cluster package.

Multi-instance coordination primitives:

    InstanceRegistry        Heartbeat + active-instance roster.
    SessionAffinity         peer_id -> owning instance_id mapping.
    CrossInstanceRelay      Pub/sub bus for forwarding frames between
                            rendezvous instances when a client lands on
                            instance B but the tunnel lives on instance A.
"""

from .instance_registry import InstanceRegistry  # noqa: F401
from .affinity import SessionAffinity  # noqa: F401
from .cross_instance_relay import CrossInstanceRelay  # noqa: F401

__all__ = [
    "InstanceRegistry",
    "SessionAffinity",
    "CrossInstanceRelay",
]
