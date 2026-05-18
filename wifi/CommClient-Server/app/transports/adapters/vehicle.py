"""
Vehicle/transport network adapter.
Detects CAN bus and V2X interfaces.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class VehicleAdapter(BaseTransportAdapter):
    """Vehicle/transport adapter for CAN, V2X, railway."""

    family = "transport_vehicle"
    display_name = "Vehicle/Transport (CAN/V2X/Railway)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect vehicle communication interfaces.

        Returns:
            List of detected vehicle interfaces
        """
        detected = []

        try:
            detected = await self._detect_can_interfaces()
        except Exception as e:
            logger.error("vehicle_detection_failed", error=str(e))

        logger.info("vehicle_detection_complete", count=len(detected))
        return detected

    async def _detect_can_interfaces(self) -> list[dict[str, Any]]:
        """Detect CAN bus interfaces."""
        detected = []

        try:
            import psutil

            if_stats = psutil.net_if_stats()

            for iface_name in if_stats.keys():
                if iface_name.startswith("can") or "can" in iface_name.lower():
                    detected.append(
                        {
                            "interface": iface_name,
                            "type": "can",
                            "status": "available",
                            "metadata": {
                                "medium": "serial_bus",
                                "protocol": "CAN",
                            },
                        }
                    )
                elif "vcan" in iface_name.lower():
                    detected.append(
                        {
                            "interface": iface_name,
                            "type": "virtual_can",
                            "status": "available",
                            "metadata": {
                                "medium": "virtual",
                                "protocol": "CAN (Virtual)",
                            },
                        }
                    )

        except Exception as e:
            logger.warning("can_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to vehicle interface."""
        try:
            import socket

            # Create CAN socket
            sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            sock.bind((interface,))
            logger.info("vehicle_connected", interface=interface, protocol="CAN")
            return sock
        except Exception as e:
            logger.error("vehicle_connect_failed", interface=interface, error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from vehicle interface."""
        try:
            if hasattr(connection_id, "close"):
                connection_id.close()
                logger.info("vehicle_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("vehicle_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send CAN frame."""
        try:
            if hasattr(connection_id, "send"):
                sent = connection_id.send(data)
                logger.debug("vehicle_sent", bytes=sent)
                return sent
            return 0
        except Exception as e:
            logger.error("vehicle_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive CAN frame."""
        try:
            if hasattr(connection_id, "recv"):
                data = connection_id.recv(buffer_size)
                logger.debug("vehicle_received", bytes=len(data))
                return data
            return b""
        except Exception as e:
            logger.error("vehicle_receive_failed", error=str(e))
            return b""
