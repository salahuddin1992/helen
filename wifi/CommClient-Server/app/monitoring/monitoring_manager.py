"""Monitoring manager — top-level lifecycle orchestrator.

Starts and stops the per-concern background loops in the right
order:

    health_checker  → metrics_collector → topology_snapshot → alert_manager

Each loop is independent; the manager exists purely to give callers
a single ``start_monitoring()`` / ``stop_monitoring()`` entry point.
"""

from __future__ import annotations

from app.core.logging import get_logger

from app.monitoring.alert_manager import get_alert_manager
from app.monitoring.dashboard_renderer import render_json
from app.monitoring.health_checker import get_health_checker
from app.monitoring.metrics_collector import get_metrics_collector
from app.monitoring.monitoring_events import history
from app.monitoring.topology_snapshot import get_topology_capturer

logger = get_logger(__name__)


class MonitoringManager:
    _singleton: "MonitoringManager | None" = None

    def __init__(self) -> None:
        self._started = False

    @classmethod
    def instance(cls) -> "MonitoringManager":
        if cls._singleton is None:
            cls._singleton = MonitoringManager()
        return cls._singleton

    def start(self) -> None:
        if self._started:
            return
        # Install webhook dispatcher BEFORE alerts so it catches the
        # first wave of events emitted on the bus.
        try:
            from app.monitoring.webhook_dispatcher import install as install_webhooks
            install_webhooks()
        except Exception as e:
            logger.warning("monitoring_webhook_install_failed", error=str(e))
        get_health_checker().start()
        get_metrics_collector().start()
        get_topology_capturer().start()
        get_alert_manager().start()
        self._started = True
        logger.info("monitoring_manager_started")

    def stop(self) -> None:
        if not self._started:
            return
        get_alert_manager().stop()
        get_topology_capturer().stop()
        get_metrics_collector().stop()
        get_health_checker().stop()
        self._started = False
        logger.info("monitoring_manager_stopped")

    def snapshot(self) -> dict:
        return {
            "started": self._started,
            "state":   render_json(),
            "events":  history(limit=50),
        }


def get_monitoring_manager() -> MonitoringManager:
    return MonitoringManager.instance()


def start_monitoring() -> None:
    get_monitoring_manager().start()


def stop_monitoring() -> None:
    get_monitoring_manager().stop()
