"""
Security/isolated network adapter.
Detects data diodes and air-gap staging interfaces.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class SecurityIsolatedAdapter(BaseTransportAdapter):
    """Security isolated adapter for data diodes and air-gaps."""

    family = "security_isolated"
    display_name = "Security Isolated (Data Diode/Air-Gap)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect data diode and air-gap interfaces.

        Returns:
            List of detected isolated interfaces
        """
        detected = []

        try:
            # Data diodes are typically detected manually or via specialized hardware
            # This is mostly a placeholder for enterprise security appliances
            detected = await self._detect_data_diodes()
        except Exception as e:
            logger.error("security_isolated_detection_failed", error=str(e))

        logger.info("security_isolated_detection_complete", count=len(detected))
        return detected

    async def _detect_data_diodes(self) -> list[dict[str, Any]]:
        """Detect data diode interfaces."""
        detected = []

        try:
            # Known data diode vendor NICs
            import psutil

            if_stats = psutil.net_if_stats()

            for iface_name in if_stats.keys():
                # Look for specific data diode indicators in interface names
                if "diode" in iface_name.lower() or "airgap" in iface_name.lower():
                    detected.append(
                        {
                            "interface": iface_name,
                            "type": "data_diode",
                            "status": "available",
                            "metadata": {
                                "medium": "wired",
                                "protocol": "Data Diode",
                                "isolated": True,
                            },
                        }
                    )

        except Exception as e:
            logger.warning("data_diode_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to isolated interface."""
        try:
            logger.info("security_isolated_connecting", interface=interface)
            # Data diodes have unidirectional nature - log appropriately
            direction = config.get("direction", "bidirectional")
            logger.info("security_isolated_direction", direction=direction)
            return interface
        except Exception as e:
            logger.error("security_isolated_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from isolated interface."""
        try:
            logger.info("security_isolated_disconnected")
            return True
        except Exception as e:
            logger.error("security_isolated_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data (may be restricted by data diode)."""
        # Data diodes typically only allow one direction
        logger.debug("security_isolated_send_attempted", bytes=len(data))
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data (may be restricted by data diode)."""
        # Data diodes typically only allow one direction
        logger.debug("security_isolated_receive_attempted")
        return b""
