"""
Tactical/emergency transport adapter.
Detects P25/FirstNet/tactical radio gateways.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class TacticalAdapter(BaseTransportAdapter):
    """Tactical/emergency adapter for P25 and FirstNet."""

    family = "tactical_emergency"
    display_name = "Tactical/Emergency (P25/FirstNet)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect tactical/emergency radio gateways.

        Returns:
            List of detected tactical interfaces
        """
        detected = []

        try:
            detected = await self._detect_tactical_gateways()
        except Exception as e:
            logger.error("tactical_detection_failed", error=str(e))

        logger.info("tactical_detection_complete", count=len(detected))
        return detected

    async def _detect_tactical_gateways(self) -> list[dict[str, Any]]:
        """Detect tactical radio gateways."""
        detected = []

        try:
            import serial.tools.list_ports

            tactical_keywords = ["p25", "firstnet", "psap", "apco", "radio gateway"]

            for port, desc, hwid in serial.tools.list_ports.comports():
                if any(kw in desc.lower() for kw in tactical_keywords):
                    protocol = "P25"
                    if "firstnet" in desc.lower():
                        protocol = "FirstNet (LTE)"

                    detected.append(
                        {
                            "interface": port,
                            "port": port,
                            "protocol": protocol,
                            "description": desc,
                            "status": "available",
                            "metadata": {
                                "medium": "wireless",
                                "protocol": protocol,
                                "emergency": True,
                            },
                        }
                    )

        except ImportError:
            logger.debug("pyserial_not_available")
        except Exception as e:
            logger.warning("tactical_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to tactical gateway."""
        try:
            import serial

            baudrate = config.get("baudrate", 9600)
            timeout = config.get("timeout", 1.0)

            conn = serial.Serial(interface, baudrate, timeout=timeout)
            logger.info("tactical_connected", interface=interface, protocol=config.get("protocol"))
            return conn
        except ImportError:
            logger.error("pyserial_not_available")
            raise
        except Exception as e:
            logger.error("tactical_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from tactical gateway."""
        try:
            if hasattr(connection_id, "close"):
                connection_id.close()
                logger.info("tactical_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("tactical_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data to tactical gateway."""
        try:
            if hasattr(connection_id, "write"):
                sent = connection_id.write(data)
                logger.debug("tactical_sent", bytes=sent)
                return sent
            return 0
        except Exception as e:
            logger.error("tactical_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from tactical gateway."""
        try:
            if hasattr(connection_id, "read"):
                data = connection_id.read(buffer_size)
                logger.debug("tactical_received", bytes=len(data))
                return data
            return b""
        except Exception as e:
            logger.error("tactical_receive_failed", error=str(e))
            return b""
