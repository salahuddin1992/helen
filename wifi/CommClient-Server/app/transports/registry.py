"""
Transport Registry — centralized catalog of available network transports.
Loads transport definitions and detection rules from configuration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.transports.types import (
    DetectionMethod,
    LatencyClass,
    SecurityLevel,
    TransportCategory,
    TransportDefinition,
    TransportMedium,
)

logger = get_logger(__name__)


class TransportRegistry:
    """
    Singleton registry of all available network transport types.
    Manages transport definitions, categories, and detection rules.
    """

    _instance: Optional[TransportRegistry] = None
    _transports: dict[str, TransportDefinition] = {}
    _detection_rules: dict[str, dict] = {}

    def __new__(cls) -> TransportRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        self._initialized = True
        self._load_transports()
        logger.info("Transport registry initialized", transports_loaded=len(self._transports))

    def _load_transports(self) -> None:
        """Load transport definitions from configuration file or create defaults."""
        config_dir = Path(__file__).parent / "config"
        transport_file = config_dir / "transport_catalog.json"

        if transport_file.exists():
            try:
                with open(transport_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Handle both list format and {transports: [...]} format
                entries = data if isinstance(data, list) else data.get("transports", [])

                for transport_data in entries:
                    try:
                        # Map category string to enum, fallback to CUSTOM
                        cat_val = transport_data.get("category", "custom")
                        try:
                            TransportCategory(cat_val)
                        except ValueError:
                            transport_data["category"] = "custom"

                        transport = TransportDefinition(**transport_data)
                        self._transports[transport.id] = transport
                    except Exception as e:
                        logger.debug("Skipped transport entry", error=str(e),
                                     id=transport_data.get("id", "?"))
            except Exception as e:
                logger.warning("Failed to load transport catalog", error=str(e))

        # Load detection rules
        rules_file = config_dir / "detection_rules.json"
        if rules_file.exists():
            try:
                with open(rules_file, "r", encoding="utf-8") as f:
                    self._detection_rules = json.load(f)
            except Exception as e:
                logger.warning("Failed to load detection rules", error=str(e))

        # If no transports loaded, create defaults
        if not self._transports:
            self._create_default_transports()

    def _create_default_transports(self) -> None:
        """Create default transport definitions."""
        defaults = [
            TransportDefinition(
                id="ethernet",
                name="Ethernet (802.3)",
                category=TransportCategory.ETHERNET,
                description="Wired Ethernet networks using twisted-pair copper cables",
                layer=2,
                medium=TransportMedium.WIRED,
                typical_bandwidth="1-100 Gbps",
                typical_range="100 meters",
                latency_class=LatencyClass.ULTRA_LOW,
                detection_method=DetectionMethod.INTERFACE_SCAN,
                adapter_family="ethernet",
                is_common=True,
                requires_hardware=True,
                duplex="full",
                supports_multicast=True,
                supports_broadcast=True,
                max_nodes=None,
                security_level=SecurityLevel.BASIC,
            ),
            TransportDefinition(
                id="wifi_80211",
                name="Wi-Fi (802.11a/b/g/n)",
                category=TransportCategory.WIFI_80211,
                description="Wireless LAN using 2.4GHz and 5GHz bands",
                layer=2,
                medium=TransportMedium.WIRELESS,
                typical_bandwidth="6-600 Mbps",
                typical_range="50-100 meters",
                latency_class=LatencyClass.LOW,
                detection_method=DetectionMethod.DRIVER_CHECK,
                adapter_family="wifi",
                is_common=True,
                requires_hardware=True,
                duplex="half",
                supports_multicast=True,
                supports_broadcast=True,
                max_nodes=None,
                security_level=SecurityLevel.MEDIUM,
            ),
            TransportDefinition(
                id="wifi_6",
                name="Wi-Fi 6 (802.11ax)",
                category=TransportCategory.WIFI_80211,
                description="High-efficiency wireless LAN with improved throughput and latency",
                layer=2,
                medium=TransportMedium.WIRELESS,
                typical_bandwidth="600 Mbps - 9.6 Gbps",
                typical_range="50-150 meters",
                latency_class=LatencyClass.LOW,
                detection_method=DetectionMethod.DRIVER_CHECK,
                adapter_family="wifi6",
                is_common=False,
                requires_hardware=True,
                duplex="full",
                supports_multicast=True,
                supports_broadcast=True,
                max_nodes=None,
                security_level=SecurityLevel.HIGH,
            ),
            TransportDefinition(
                id="bluetooth_le",
                name="Bluetooth Low Energy (BLE)",
                category=TransportCategory.BLE,
                description="Short-range wireless communication for IoT and wearables",
                layer=2,
                medium=TransportMedium.WIRELESS,
                typical_bandwidth="1 Mbps",
                typical_range="10-240 meters",
                latency_class=LatencyClass.LOW,
                detection_method=DetectionMethod.HARDWARE_PROBE,
                adapter_family="bluetooth",
                is_common=True,
                requires_hardware=True,
                duplex="full",
                supports_multicast=False,
                supports_broadcast=True,
                max_nodes=7,
                security_level=SecurityLevel.MEDIUM,
            ),
            TransportDefinition(
                id="usb",
                name="USB Network Adapter",
                category=TransportCategory.USB,
                description="USB-based network interface (tethering, adapters)",
                layer=2,
                medium=TransportMedium.WIRED,
                typical_bandwidth="5-480 Mbps",
                typical_range="5 meters",
                latency_class=LatencyClass.ULTRA_LOW,
                detection_method=DetectionMethod.HARDWARE_PROBE,
                adapter_family="usb",
                is_common=True,
                requires_hardware=True,
                duplex="full",
                supports_multicast=True,
                supports_broadcast=True,
                max_nodes=None,
                security_level=SecurityLevel.BASIC,
            ),
            TransportDefinition(
                id="serial",
                name="Serial (RS-232/422/485)",
                category=TransportCategory.SERIAL_RS232,
                description="Serial port communication for industrial and legacy devices",
                layer=1,
                medium=TransportMedium.WIRED,
                typical_bandwidth="9.6-115.2 kbps",
                typical_range="15 meters",
                latency_class=LatencyClass.MEDIUM,
                detection_method=DetectionMethod.INTERFACE_SCAN,
                adapter_family="serial",
                is_common=True,
                requires_hardware=False,
                duplex="full",
                supports_multicast=False,
                supports_broadcast=False,
                max_nodes=32,
                security_level=SecurityLevel.NONE,
            ),
            TransportDefinition(
                id="modbus",
                name="Modbus (TCP/RTU)",
                category=TransportCategory.MODBUS,
                description="Industrial protocol for SCADA and automation systems",
                layer=7,
                medium=TransportMedium.WIRED,
                typical_bandwidth="9.6 kbps - 1 Mbps",
                typical_range="1000+ meters",
                latency_class=LatencyClass.MEDIUM,
                detection_method=DetectionMethod.PORT_SCAN,
                adapter_family="modbus",
                is_common=True,
                requires_hardware=False,
                duplex="full",
                supports_multicast=False,
                supports_broadcast=False,
                max_nodes=247,
                security_level=SecurityLevel.BASIC,
            ),
        ]

        for transport in defaults:
            self._transports[transport.id] = transport

    def get_all_transports(self) -> list[TransportDefinition]:
        """Get all registered transports."""
        return list(self._transports.values())

    def get_by_category(self, category: str) -> list[TransportDefinition]:
        """Filter transports by category."""
        try:
            cat = TransportCategory(category)
            return [t for t in self._transports.values() if t.category == cat]
        except ValueError:
            logger.warning("Invalid category", category=category)
            return []

    def get_by_adapter_family(self, family: str) -> list[TransportDefinition]:
        """Filter transports by adapter family."""
        return [t for t in self._transports.values() if t.adapter_family.lower() == family.lower()]

    def get_by_medium(self, medium: str) -> list[TransportDefinition]:
        """Filter transports by physical medium."""
        try:
            med = TransportMedium(medium)
            return [t for t in self._transports.values() if t.medium == med]
        except ValueError:
            logger.warning("Invalid medium", medium=medium)
            return []

    def get_common_transports(self) -> list[TransportDefinition]:
        """Get only commonly deployed transports."""
        return [t for t in self._transports.values() if t.is_common]

    def get_transport(self, transport_id: str) -> Optional[TransportDefinition]:
        """Get a specific transport by ID."""
        return self._transports.get(transport_id)

    def search(self, query: str) -> list[TransportDefinition]:
        """Full-text search by name and description."""
        query_lower = query.lower()
        results = []
        for transport in self._transports.values():
            if (query_lower in transport.name.lower() or
                query_lower in transport.description.lower() or
                query_lower in transport.id.lower()):
                results.append(transport)
        return results

    def get_categories(self) -> list[dict]:
        """Get list of categories with counts."""
        categories = {}
        for transport in self._transports.values():
            cat_name = transport.category.value
            if cat_name not in categories:
                categories[cat_name] = 0
            categories[cat_name] += 1

        return [
            {"category": cat, "count": count}
            for cat, count in sorted(categories.items())
        ]

    def get_detection_rules(self, adapter_family: str) -> dict:
        """Get detection rules for a specific adapter family."""
        return self._detection_rules.get(adapter_family, {})

    def get_statistics(self) -> dict:
        """Get registry statistics."""
        by_category = {}
        by_medium = {}
        common_count = 0

        for transport in self._transports.values():
            cat = transport.category.value
            by_category[cat] = by_category.get(cat, 0) + 1

            med = transport.medium.value
            by_medium[med] = by_medium.get(med, 0) + 1

            if transport.is_common:
                common_count += 1

        return {
            "total_transports": len(self._transports),
            "by_category": by_category,
            "by_medium": by_medium,
            "common_count": common_count,
            "categories_count": len(by_category),
            "mediums_count": len(by_medium),
        }
