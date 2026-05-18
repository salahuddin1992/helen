"""
Satellite/aerospace transport adapter.
Detects satellite modems and GPS receivers.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class SatelliteAdapter(BaseTransportAdapter):
    """Satellite/aerospace adapter for satellite and GPS links."""

    family = "satellite_aerospace"
    display_name = "Satellite/Aerospace"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect satellite modems and GPS receivers.

        Returns:
            List of detected satellite devices
        """
        detected = []

        try:
            detected = await self._detect_serial_satellite_devices()
        except Exception as e:
            logger.error("satellite_detection_failed", error=str(e))

        logger.info("satellite_detection_complete", count=len(detected))
        return detected

    async def _detect_serial_satellite_devices(self) -> list[dict[str, Any]]:
        """Detect satellite modems on serial ports."""
        detected = []

        try:
            import serial.tools.list_ports

            sat_keywords = ["satellite", "gps", "gnss", "inmarsat"]

            for port, desc, hwid in serial.tools.list_ports.comports():
                if any(kw in desc.lower() for kw in sat_keywords):
                    detected.append(
                        {
                            "interface": port,
                            "port": port,
                            "type": "satellite",
                            "description": desc,
                            "status": "available",
                            "metadata": {
                                "medium": "satellite",
                                "protocol": "Satellite",
                            },
                        }
                    )

        except ImportError:
            logger.debug("pyserial_not_available")
        except Exception as e:
            logger.warning("satellite_serial_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to satellite device."""
        try:
            import serial

            baudrate = config.get("baudrate", 9600)
            timeout = config.get("timeout", 1.0)

            conn = serial.Serial(interface, baudrate, timeout=timeout)
            logger.info("satellite_connected", interface=interface)
            return conn
        except ImportError:
            logger.error("pyserial_not_available")
            raise
        except Exception as e:
            logger.error("satellite_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from satellite device."""
        try:
            if hasattr(connection_id, "close"):
                connection_id.close()
                logger.info("satellite_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("satellite_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data to satellite."""
        try:
            if hasattr(connection_id, "write"):
                sent = connection_id.write(data)
                logger.debug("satellite_sent", bytes=sent)
                return sent
            return 0
        except Exception as e:
            logger.error("satellite_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from satellite."""
        try:
            if hasattr(connection_id, "read"):
                data = connection_id.read(buffer_size)
                logger.debug("satellite_received", bytes=len(data))
                return data
            return b""
        except Exception as e:
            logger.error("satellite_receive_failed", error=str(e))
            return b""
