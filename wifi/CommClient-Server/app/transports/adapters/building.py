"""
Building/campus automation adapter.
Detects BACnet, KNX, LonWorks, and DALI interfaces.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class BuildingAdapter(BaseTransportAdapter):
    """Building automation adapter for BACnet, KNX, LonWorks."""

    family = "building_campus"
    display_name = "Building Automation (BACnet/KNX/LonWorks)"

    # Known building automation ports
    BUILDING_PORTS = {
        47808: "BACnet",
        3671: "KNX",
        24000: "LonWorks",
        64: "DALI",
    }

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect building automation interfaces.

        Returns:
            List of detected automation servers
        """
        detected = []

        try:
            detected = await self._probe_building_ports()
        except Exception as e:
            logger.error("building_detection_failed", error=str(e))

        logger.info("building_detection_complete", count=len(detected))
        return detected

    async def _probe_building_ports(self) -> list[dict[str, Any]]:
        """Probe for building automation ports."""
        detected = []

        for port, protocol_name in self.BUILDING_PORTS.items():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)

                result = sock.connect_ex(("127.0.0.1", port))
                sock.close()

                if result == 0:
                    detected.append(
                        {
                            "interface": f"building_{port}",
                            "port": port,
                            "protocol": protocol_name,
                            "status": "available",
                            "metadata": {
                                "medium": "network",
                                "protocol": protocol_name,
                            },
                        }
                    )
                    logger.debug("building_port_found", port=port, protocol=protocol_name)
            except Exception as e:
                logger.debug("building_port_probe_failed", port=port)

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to building automation server."""
        try:
            host = config.get("host", "127.0.0.1")
            port = config.get("port", 47808)

            reader, writer = await asyncio.open_connection(host, port)
            logger.info("building_connected", host=host, port=port)
            return (reader, writer)
        except Exception as e:
            logger.error("building_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from building automation."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.close()
                await writer.wait_closed()
                logger.info("building_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("building_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send command to building automation."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.write(data)
                await writer.drain()
                logger.debug("building_sent", bytes=len(data))
                return len(data)
            return 0
        except Exception as e:
            logger.error("building_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from building automation."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                data = await asyncio.wait_for(reader.read(buffer_size), timeout=1.0)
                logger.debug("building_received", bytes=len(data))
                return data
            return b""
        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            logger.error("building_receive_failed", error=str(e))
            return b""
