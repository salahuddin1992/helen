"""Resilient-networking module — fault detection + recovery surface.

Public entry points:

    from app.resilience import (
        get_resilience_manager, start_resilience, stop_resilience,
    )
"""

from app.resilience.resilience_manager import (                  # noqa: F401
    get_resilience_manager,
    start_resilience,
    stop_resilience,
)
