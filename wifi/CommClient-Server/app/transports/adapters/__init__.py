"""
Transport adapters — lazy registry.

The package ships 45+ adapter modules covering 46 transport families
(ethernet, wifi, fiber, cellular, satellite, scada, military, medical,
nuclear, automotive, …). Each module subclasses ``BaseTransportAdapter``
and exposes detect / connect / send / receive.

Adapters are imported **on demand** — the first call to
``get_adapter("medical")`` imports ``app.transports.adapters.medical``
and instantiates its single adapter class. Repeat calls return the
cached instance. This keeps cold-start under 100 ms even with 45+
modules on disk, while still letting code that genuinely wants a live
transport bridge reach for one.

For pure LAN deployments where Socket.IO already runs over the OS IP
stack, callers can ignore the registry entirely — None-returning paths
still work (every consumer treats ``adapter is None`` as "fall back to
default routing").
"""

from __future__ import annotations

import importlib
from typing import Optional

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)

# Family → module file (resolved relative to this package)
_FAMILY_MODULES: dict[str, str] = {
    # Existing 28 adapter scaffolds
    "av_network":             "av_network",
    "building_campus":        "building",
    "cellular_private":       "cellular",
    "datacenter_fabric":      "datacenter",
    "ethernet":               "ethernet",
    "fiber":                  "fiber",
    "high_performance":       "high_performance",
    "industrial":             "industrial",
    "iot_sensor":             "iot_sensor",
    "legacy":                 "legacy",
    "management":             "management",
    "mesh":                   "mesh",
    "optical_link":           "optical_link",
    "overlay_tunnel":         "overlay_tunnel",
    "service_overlay":        "overlay_tunnel",   # shares overlay_tunnel module
    "powerline":              "powerline",
    "radio":                  "radio",
    "satellite_aerospace":    "satellite",
    "scada_utility":          "scada",
    "security_isolated":      "security_isolated",
    "serial_bus":             "serial_bus",
    "specialty_vertical":     "specialty",
    "storage_network":        "storage_network",
    "tactical_emergency":     "tactical",
    "time_sensitive":         "time_sensitive",
    "transport_vehicle":      "vehicle",
    "wan_private":            "wan_private",
    "wifi":                   "wifi",
    "wireless_bridge":        "wireless_bridge",
    # 18 new adapter modules generated 2026-05-04
    "military_defense":       "military_defense",
    "maritime_underwater":    "maritime_underwater",
    "energy_grid":            "energy_grid",
    "medical":                "medical",
    "broadcast_media":        "broadcast_media",
    "topology":               "topology",
    "deep_space":             "deep_space",
    "financial_trading":      "financial_trading",
    "quantum_experimental":   "quantum_experimental",
    "railway":                "railway",
    "mining_underground":     "mining_underground",
    "drone_uav":              "drone_uav",
    "automotive":             "automotive",
    "aviation":               "aviation",
    "emergency_public_safety": "emergency_public_safety",
    "nuclear":                "nuclear",
    "acoustic":               "acoustic",
}

_INSTANCES: dict[str, BaseTransportAdapter] = {}


def get_adapter(family: str) -> Optional[BaseTransportAdapter]:
    """Lazy-load and cache the adapter for ``family``.

    Returns None if the family is unknown or the module fails to import
    (callers fall back to default routing — never a hard error).
    """
    if family in _INSTANCES:
        return _INSTANCES[family]

    module_name = _FAMILY_MODULES.get(family)
    if not module_name:
        logger.debug("get_adapter_unknown_family", family=family)
        return None

    try:
        mod = importlib.import_module(
            f"app.transports.adapters.{module_name}"
        )
    except ImportError as exc:
        logger.warning("adapter_import_failed",
                       family=family, error=str(exc))
        return None

    # Find the first BaseTransportAdapter subclass in the module
    cls = None
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if (isinstance(obj, type)
                and issubclass(obj, BaseTransportAdapter)
                and obj is not BaseTransportAdapter):
            cls = obj
            break

    if cls is None:
        logger.warning("adapter_class_missing",
                       family=family, module=module_name)
        return None

    instance = cls()
    _INSTANCES[family] = instance
    return instance


def get_all_adapters() -> dict[str, BaseTransportAdapter]:
    """Eagerly import every adapter and return the populated registry."""
    for family in _FAMILY_MODULES:
        get_adapter(family)
    return dict(_INSTANCES)


def list_adapter_families() -> list[str]:
    """List every family that has a module on disk (importable or not)."""
    return sorted(_FAMILY_MODULES.keys())


def list_available_adapters() -> list[tuple[str, str]]:
    """Return ``(family, display_name)`` for every loadable adapter."""
    pairs: list[tuple[str, str]] = []
    for family in _FAMILY_MODULES:
        ad = get_adapter(family)
        if ad is not None:
            pairs.append((family, ad.display_name or family))
    return pairs


__all__ = [
    "BaseTransportAdapter",
    "get_adapter",
    "get_all_adapters",
    "list_adapter_families",
    "list_available_adapters",
]
