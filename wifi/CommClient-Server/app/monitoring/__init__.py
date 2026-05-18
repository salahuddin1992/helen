"""Monitoring package — observability layer for the mesh.

Composes health checks, metrics collection, latency tracking,
threshold alerts, and topology snapshots into a single observable
surface. Each concern lives in its own module so a future operator
can hot-swap a single piece (e.g. replace the dashboard renderer
without touching alert thresholds).

Public API:

    from app.monitoring import (
        get_monitoring_manager, start_monitoring, stop_monitoring,
    )
"""

from app.monitoring.monitoring_manager import (                  # noqa: F401
    get_monitoring_manager,
    start_monitoring,
    stop_monitoring,
)
