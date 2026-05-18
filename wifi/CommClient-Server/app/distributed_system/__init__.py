"""Distributed Systems package — facade + new primitives.

This package is *additive*: it organises the cluster-coordination
concerns into a clean package while delegating heavy lifting to the
already-shipped services in ``app/services/``.

Public entry points:

    from app.distributed_system import (
        get_cluster_manager, get_distributed_manager,
        start_distributed_system, stop_distributed_system,
    )

The package never replaces the underlying services — it composes
them so callers see one coherent distributed-system surface.
"""

from app.distributed_system.cluster_manager import (              # noqa: F401
    get_cluster_manager,
)
from app.distributed_system.distributed_manager import (          # noqa: F401
    get_distributed_manager,
    start_distributed_system,
    stop_distributed_system,
)
