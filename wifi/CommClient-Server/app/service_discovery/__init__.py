"""Service Discovery — production-grade endpoint resolution.

Public entry points:

    from app.service_discovery import (
        get_discovery_manager, start_discovery, stop_discovery,
    )

The package is the authoritative answer to "who do I talk to?"
across LAN / cluster / federation. All the underlying mesh primitives
(routing_strategy, p2p, topology, resilience) consume this through a
stable façade so a future swap of the backend (e.g. Consul, Eureka)
won't ripple through the codebase.
"""

from app.service_discovery.service_discovery_manager import (    # noqa: F401
    get_discovery_manager,
    start_discovery,
    stop_discovery,
)
