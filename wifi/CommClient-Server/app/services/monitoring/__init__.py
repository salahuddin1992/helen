"""
Monitoring service package.

Exposes:
- MetricsCollector       — system + transport metric collection with rolling window
- ConnectionRegistry     — in-memory live connections registry (Socket.IO integration)
- MetricsWebSocketManager — broadcaster for WebSocket clients on /ws/metrics

Importing this package is side-effect free; collectors must be started
explicitly from the FastAPI lifespan handler.
"""

from __future__ import annotations

from app.services.monitoring.metrics_collector import (
    MetricsCollector,
    get_metrics_collector,
)
from app.services.monitoring.connection_registry import (
    ConnectionInfo,
    ConnectionRegistry,
    get_connection_registry,
)
from app.services.monitoring.ws_streamer import (
    MetricsWebSocketManager,
    get_ws_manager,
)

__all__ = [
    "MetricsCollector",
    "get_metrics_collector",
    "ConnectionInfo",
    "ConnectionRegistry",
    "get_connection_registry",
    "MetricsWebSocketManager",
    "get_ws_manager",
]
