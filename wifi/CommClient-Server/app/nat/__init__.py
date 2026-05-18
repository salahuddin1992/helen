"""NAT Traversal module — connect peers behind routers / firewalls.

Public entry points:

    from app.nat import (
        get_nat_manager, start_nat, stop_nat,
    )
"""

from app.nat.nat_traversal_manager import (                     # noqa: F401
    get_nat_manager,
    start_nat,
    stop_nat,
)
