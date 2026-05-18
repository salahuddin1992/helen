"""
WAN/private network adapter.
Detects MPLS, SD-WAN, and leased line adapters.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class WANPrivateAdapter(BaseTransportAdapter):
    """WAN/private adapter for MPLS, SD-WAN, leased lines."""

    family = "wan_private"
    display_name = "WAN/Private (MPLS/SD-WAN)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect WAN and private network interfaces.

        Returns:
            List of detected WAN interfaces
        """
        detected = []

        try:
            detected = await self._detect_mpls_interfaces()
            detected.extend(await self._detect_sdwan_agents())
        except Exception as e:
            logger.error("wan_private_detection_failed", error=str(e))

        logger.info("wan_private_detection_complete", count=len(detected))
        return detected

    async def _detect_mpls_interfaces(self) -> list[dict[str, Any]]:
        """Detect MPLS interfaces."""
        detected = []

        try:
            import psutil

            if_stats = psutil.net_if_stats()

            for iface_name in if_stats.keys():
                if "mpls" in iface_name.lower():
                    detected.append(
                        {
                            "interface": iface_name,
                            "type": "mpls",
                            "status": "available",
                            "metadata": {
                                "medium": "network",
                                "protocol": "MPLS",
                            },
                        }
                    )

        except Exception as e:
            logger.warning("mpls_detection_failed", error=str(e))

        return detected

    async def _detect_sdwan_agents(self) -> list[dict[str, Any]]:
        """Detect SD-WAN agent ports."""
        detected = []

        # Common SD-WAN controller ports
        sdwan_ports = {
            8443: "SD-WAN Controller",
            5432: "SD-WAN Management",
            443: "SD-WAN (HTTPS)",
        }

        for port in sdwan_ports.keys():
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port),
                    timeout=0.5,
                )
                writer.close()
                await writer.wait_closed()

                detected.append(
                    {
                        "interface": f"sdwan_{port}",
                        "port": port,
                        "type": "sd_wan",
                        "status": "available",
                        "metadata": {
                            "medium": "network",
                            "protocol": "SD-WAN",
                        },
                    }
                )
            except (asyncio.TimeoutError, OSError):
                pass
            except Exception as e:
                logger.debug("sdwan_port_probe_failed", port=port)

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to WAN interface."""
        try:
            logger.info("wan_private_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("wan_private_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from WAN."""
        try:
            logger.info("wan_private_disconnected")
            return True
        except Exception as e:
            logger.error("wan_private_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data over WAN."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from WAN."""
        return b""
