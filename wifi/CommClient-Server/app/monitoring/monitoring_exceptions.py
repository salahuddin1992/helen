"""Custom exceptions for the monitoring package."""

from __future__ import annotations


class MonitoringError(Exception):
    """Base class for every monitoring exception."""


class HealthCheckError(MonitoringError):
    """A health probe could not be executed or interpreted."""


class MetricCollectionError(MonitoringError):
    """A metric collector raised on an aggregation pass."""


class AlertConfigError(MonitoringError):
    """Alert rule definition is invalid."""


class DashboardRenderError(MonitoringError):
    """Renderer could not produce output (template error, etc.)."""
