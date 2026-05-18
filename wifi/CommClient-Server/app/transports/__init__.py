"""
Network Transport Abstraction Layer.

Auto-detects available network transports and creates communication bridges.
Provides comprehensive transport discovery, quality analysis, and capability assessment.

Main Components:
- TransportRegistry: Central catalog of transport types
- TransportDetector: Discovers available transports on the system
- BridgeManager: Creates and manages communication bridges
- SignalAnalyzer: Measures network quality metrics
- TransportCapabilities: Evaluates service compatibility
- Types: Pydantic models for all data structures
"""

from __future__ import annotations

from app.transports.bridge import BridgeManager
from app.transports.capabilities import TransportCapabilities
from app.transports.detector import TransportDetector
from app.transports.registry import TransportRegistry
from app.transports.signal import SignalAnalyzer
from app.transports.types import (
    BridgeConfig,
    BridgeStatus,
    DetectedTransport,
    DetectionMethod,
    LatencyClass,
    SecurityLevel,
    SignalQuality,
    TransportCategory,
    TransportDefinition,
    TransportMedium,
    TransportStatus,
)

__all__ = [
    # Classes
    "TransportRegistry",
    "TransportDetector",
    "BridgeManager",
    "SignalAnalyzer",
    "TransportCapabilities",
    # Types
    "TransportDefinition",
    "DetectedTransport",
    "BridgeConfig",
    "BridgeStatus",
    "SignalQuality",
    # Enums
    "TransportMedium",
    "LatencyClass",
    "SecurityLevel",
    "DetectionMethod",
    "TransportStatus",
    "TransportCategory",
]
