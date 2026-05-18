"""Per-(client, service) latency tracker.

Maintains an EWMA latency per (host:port). Reuses
``services.path_health`` as the source of truth so we don't double-
count probes; this module only exposes the interface that the
discovery package needs (``latency_for(record)``).
"""

from __future__ import annotations

from typing import Optional

from app.service_discovery.service_record import ServiceRecord


def latency_for(record: ServiceRecord) -> Optional[float]:
    """Return EWMA latency in milliseconds, or None if unknown."""
    if not record.host or record.port <= 0:
        return None
    try:
        from app.services.path_health import get_path_health
        snap = get_path_health().snapshot()
        for p in snap.get("paths", []):
            if p.get("key") == f"{record.host}:{record.port}":
                ms = float(p.get("latency_ms") or 0.0)
                return ms if ms > 0 else None
    except Exception:
        return None
    return None


def latency_score(record: ServiceRecord) -> float:
    """Convert latency into a 0..1 score (higher = faster). Records
    with no measured latency get a neutral 0.6 — better than 0.5 so
    untested-but-advertised candidates aren't completely demoted."""
    ms = latency_for(record)
    if ms is None:
        # Use the advertised hint when present.
        if record.advertised_latency_ms > 0:
            ms = record.advertised_latency_ms
        else:
            return 0.6
    if ms <= 5:
        return 1.0
    if ms <= 50:
        return 0.9
    if ms <= 200:
        return max(0.4, 1.0 - (ms - 50) / 200.0)
    if ms <= 500:
        return 0.3
    return 0.1
