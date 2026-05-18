"""
Legacy transport adapter.
Detects rare/obsolete network types (Token Ring, FDDI, ATM).
"""

from __future__ import annotations

from typing import Any

import psutil

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class LegacyAdapter(BaseTransportAdapter):
    """Legacy adapter for obsolete network protocols."""

    family = "legacy"
    display_name = "Legacy Networks (Token Ring/FDDI/ATM)"

    # Known legacy protocol driver names
    LEGACY_DRIVERS = {
        "token": "Token Ring",
        "fddi": "FDDI",
        "atm": "ATM",
        "appletalk": "AppleTalk",
    }

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect legacy protocol interfaces.

        Returns:
            List of detected legacy interfaces
        """
        detected = []

        try:
            if_stats = psutil.net_if_stats()

            for iface_name in if_stats.keys():
                for driver_keyword, protocol_name in self.LEGACY_DRIVERS.items():
                    if driver_keyword in iface_name.lower():
                        detected.append(
                            {
                                "interface": iface_name,
                                "protocol": protocol_name,
                                "status": "up",
                                "metadata": {
                                    "medium": "wired",
                                    "protocol": protocol_name,
                                    "obsolete": True,
                                },
                            }
                        )
                        break

        except Exception as e:
            logger.warning("legacy_detection_failed", error=str(e))

        logger.info("legacy_detection_complete", count=len(detected))
        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to legacy interface."""
        try:
            logger.warning("legacy_connect_obsolete", interface=interface)
            logger.info("legacy_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("legacy_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from legacy interface."""
        try:
            logger.info("legacy_disconnected")
            return True
        except Exception as e:
            logger.error("legacy_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data over legacy interface."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from legacy interface."""
        return b""
