"""Route-health adapter — bridges path_health into the strategy package.

The strategy modules need read-only access to the live latency /
failure data already maintained by ``services/path_health``. This
file exposes a stable interface so the strategy code never imports
the path_health module directly — the adapter pattern means the
underlying source can be swapped (e.g. for tests).
"""

from __future__ import annotations

from typing import Optional


class RouteHealthView:
    """Read-only view over per-(host:port) live metrics."""

    @staticmethod
    def latency_score(host: str, port: int) -> float:
        try:
            from app.services.path_health import get_path_health
            return float(get_path_health().latency_score(host, port))
        except Exception:
            return 1.0  # optimistic on missing data

    @staticmethod
    def is_failed(host: str, port: int) -> bool:
        try:
            from app.services.path_health import get_path_health
            return bool(get_path_health().is_failed(host, port))
        except Exception:
            return False

    @staticmethod
    def bandwidth_mbps(host: str, port: int) -> Optional[float]:
        try:
            from app.services.bandwidth_probe import get_bandwidth
            return get_bandwidth().get(host, port)
        except Exception:
            return None


_view: RouteHealthView | None = None


def get_health_view() -> RouteHealthView:
    global _view
    if _view is None:
        _view = RouteHealthView()
    return _view
