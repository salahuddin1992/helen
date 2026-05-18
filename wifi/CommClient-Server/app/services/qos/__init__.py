"""
QoS (Quality of Service) — Voice/Video live observability subsystem.

This package powers the *Voice/Video QoS Live View* admin panel:

    app.services.qos.stats_collector   — Rolling per-stream metric buffer.
    app.services.qos.mos_calculator    — ITU-T G.107 E-Model (R-factor → MOS).
    app.services.qos.anomaly_detector  — Threshold + temporal anomaly engine.
    app.services.qos.mesh_topology     — Mesh / SFU graph extractor.
    app.services.qos.admin_overrides   — Force-preset / force-codec / chaos.
    app.services.qos.ws_stream         — Per-call WebSocket fan-out @1-2Hz.

The collector is wired in as an *observer* to ``call_handlers`` via the
``register_qos_observer`` hook — call_handlers.py itself is NOT modified;
the observer registration happens lazily inside our own routes/services.

Public façade
-------------

Most callers should grab the singletons from here::

    from app.services.qos import (
        qos_stats_collector,
        qos_mos_calculator,
        qos_anomaly_detector,
        qos_mesh_topology,
        qos_admin_overrides,
        qos_ws_manager,
    )
"""

from __future__ import annotations

from app.services.qos.admin_overrides import QoSAdminOverrides, qos_admin_overrides
from app.services.qos.anomaly_detector import QoSAnomalyDetector, qos_anomaly_detector
from app.services.qos.mesh_topology import QoSMeshTopology, qos_mesh_topology
from app.services.qos.mos_calculator import MOSCalculator, qos_mos_calculator
from app.services.qos.stats_collector import QoSStatsCollector, qos_stats_collector
from app.services.qos.ws_stream import QoSWebSocketManager, qos_ws_manager

__all__ = [
    "MOSCalculator",
    "qos_mos_calculator",
    "QoSStatsCollector",
    "qos_stats_collector",
    "QoSAnomalyDetector",
    "qos_anomaly_detector",
    "QoSMeshTopology",
    "qos_mesh_topology",
    "QoSAdminOverrides",
    "qos_admin_overrides",
    "QoSWebSocketManager",
    "qos_ws_manager",
]
