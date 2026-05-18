"""
Lightweight in-memory metrics collector for server observability.
Tracks counters for key system events and provides uptime calculation.
Thread-safe via asyncio.Lock for atomic counter increments.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone

from app.core.logging import get_logger

logger = get_logger(__name__)


_RATE_WINDOW_SEC = 60.0


class MetricsService:
    """In-memory metrics aggregator — counters, rate windows, uptime."""

    def __init__(self):
        self._start_time: datetime = datetime.now(timezone.utc)
        self._lock = asyncio.Lock()

        # Counters — initialized to zero
        self._counters: dict[str, int] = {
            "messages_sent_total": 0,
            "calls_initiated_total": 0,
            "files_uploaded_total": 0,
            "socket_connections_total": 0,
            "api_requests_total": 0,
        }
        # Per-counter rolling window of (timestamp, increment) for the
        # last ``_RATE_WINDOW_SEC`` seconds. Used by get_rate() to
        # compute per-second rates without a separate timeseries DB.
        self._rate_windows: dict[str, deque[tuple[float, int]]] = {}

    async def increment(self, metric_name: str, value: int = 1) -> None:
        """
        Atomically increment a counter by the given value (default 1).
        Creates new counter if it doesn't exist (for extensibility).
        """
        if not isinstance(value, int) or value < 0:
            logger.warning("invalid_metric_increment", metric_name=metric_name, value=value)
            return

        now = time.time()
        async with self._lock:
            if metric_name not in self._counters:
                self._counters[metric_name] = 0
            self._counters[metric_name] += value
            window = self._rate_windows.setdefault(metric_name, deque())
            window.append((now, value))
            cutoff = now - _RATE_WINDOW_SEC
            while window and window[0][0] < cutoff:
                window.popleft()

    async def get_rate(self, metric_name: str, window_sec: float = 60.0) -> float:
        """Average increments per second over the requested window
        (capped at the rolling window size). Returns 0 if no samples."""
        if window_sec <= 0:
            return 0.0
        now = time.time()
        cutoff = now - min(window_sec, _RATE_WINDOW_SEC)
        async with self._lock:
            window = self._rate_windows.get(metric_name)
            if not window:
                return 0.0
            total = sum(v for ts, v in window if ts >= cutoff)
        actual_window = min(window_sec, _RATE_WINDOW_SEC)
        return float(total) / actual_window

    async def get_all(self) -> dict[str, int]:
        """Return a snapshot of all metric counters."""
        async with self._lock:
            return dict(self._counters)

    async def get_all_with_rates(self) -> dict[str, dict]:
        """Snapshot every counter alongside its 60-second rate."""
        async with self._lock:
            counters = dict(self._counters)
            rates = {}
            now = time.time()
            cutoff = now - _RATE_WINDOW_SEC
            for name, win in self._rate_windows.items():
                total = sum(v for ts, v in win if ts >= cutoff)
                rates[name] = total / _RATE_WINDOW_SEC
        return {
            name: {"count": counters[name], "rate_per_sec": rates.get(name, 0.0)}
            for name in counters
        }

    def get_uptime(self) -> float:
        """Return server uptime in seconds since initialization."""
        now = datetime.now(timezone.utc)
        elapsed = (now - self._start_time).total_seconds()
        return max(0.0, elapsed)

    async def reset(self) -> None:
        """
        Reset all counters to zero.
        Intended for testing; do not use in production.
        """
        async with self._lock:
            for key in self._counters:
                self._counters[key] = 0
        logger.warning("metrics_reset", uptime_seconds=self.get_uptime())


# Singleton instance
metrics_service = MetricsService()
