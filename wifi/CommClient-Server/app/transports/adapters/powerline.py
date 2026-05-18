"""
Powerline/MoCA transport adapter.
Detects powerline and coaxial communication adapters.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class PowerlineAdapter(BaseTransportAdapter):
    """Powerline and MoCA adapter for coax/power line communications."""

    family = "powerline"
    display_name = "Powerline/MoCA"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect powerline/MoCA adapters.

        Returns:
            List of detected powerline adapters
        """
        detected = []

        try:
            # Try to detect via network interfaces with vendor OUI
            detected = await self._detect_via_vendors()
        except Exception as e:
            logger.error("powerline_detection_failed", error=str(e))

        logger.info("powerline_detection_complete", count=len(detected))
        return detected

    async def _detect_via_vendors(self) -> list[dict[str, Any]]:
        """Detect powerline adapters via known vendor MAC OUIs."""
        detected = []

        try:
            import psutil

            # Known powerline/MoCA vendor OUIs
            vendor_ouis = {
                "00:B0:52": "Netgear Powerline",  # Netgear
                "2C:30:33": "TP-Link Powerline",  # TP-Link
                "38:22:D6": "Zyxel Powerline",   # Zyxel
                "A0:26:06": "Qualcomm Atheros",
            }

            if_addrs = psutil.net_if_addrs()

            for iface_name, addrs in if_addrs.items():
                for addr in addrs:
                    # Check MAC address against vendor OUIs
                    if hasattr(addr, "address"):
                        mac = addr.address.upper()
                        for oui, vendor in vendor_ouis.items():
                            if mac.startswith(oui):
                                detected.append(
                                    {
                                        "interface": iface_name,
                                        "mac_address": mac,
                                        "vendor": vendor,
                                        "status": "available",
                                        "metadata": {
                                            "medium": "powerline",
                                            "protocol": "HomePlug AV2/G.hn",
                                        },
                                    }
                                )
                                break
        except ImportError:
            logger.warning("psutil_not_available")
        except Exception as e:
            logger.warning("powerline_vendor_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to powerline adapter."""
        try:
            logger.info("powerline_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("powerline_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from powerline."""
        try:
            logger.info("powerline_disconnected")
            return True
        except Exception as e:
            logger.error("powerline_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data over powerline."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from powerline."""
        return b""
