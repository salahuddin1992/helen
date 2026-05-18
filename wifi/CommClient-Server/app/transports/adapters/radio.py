"""
Radio transport adapter.
Detects radio modems (TETRA, DMR, P25) and gateways.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class RadioAdapter(BaseTransportAdapter):
    """Radio modem adapter for professional radio protocols."""

    family = "radio"
    display_name = "Radio Modem"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect radio modems on serial ports.

        Returns:
            List of detected radio gateways
        """
        detected = []

        try:
            if platform.system() == "Linux":
                detected = await self._detect_linux()
            else:
                detected = await self._detect_serial_ports()
        except Exception as e:
            logger.error("radio_detection_failed", error=str(e))

        logger.info("radio_detection_complete", count=len(detected))
        return detected

    async def _detect_linux(self) -> list[dict[str, Any]]:
        """Detect radio devices on Linux."""
        detected = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "ls",
                "/dev/ttyUSB*",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            devices = stdout.decode("utf-8", errors="ignore").split()
            for device in devices:
                detected.append(
                    {
                        "interface": device.split("/")[-1],
                        "port": device,
                        "type": "radio_modem",
                        "status": "available",
                        "metadata": {"medium": "radio", "protocol": "TETRA/DMR/P25"},
                    }
                )
        except Exception as e:
            logger.warning("radio_linux_detection_failed", error=str(e))

        return detected

    async def _detect_serial_ports(self) -> list[dict[str, Any]]:
        """Detect radio modems on serial ports (Windows/macOS)."""
        detected = []

        try:
            import serial.tools.list_ports

            for port, desc, hwid in serial.tools.list_ports.comports():
                if any(radio_indicator in desc.lower() for radio_indicator in ["radio", "tetra", "dmr", "p25"]):
                    detected.append(
                        {
                            "interface": port,
                            "port": port,
                            "type": "radio_modem",
                            "status": "available",
                            "description": desc,
                            "metadata": {
                                "medium": "radio",
                                "protocol": "TETRA/DMR/P25",
                                "hwid": hwid,
                            },
                        }
                    )
        except ImportError:
            logger.warning("serial_tools_not_available")
        except Exception as e:
            logger.warning("radio_serial_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """
        Connect to radio modem.

        Args:
            interface: Serial port (e.g., /dev/ttyUSB0)
            config: Baud rate, timeout, etc.

        Returns:
            Serial connection handle
        """
        try:
            import serial

            baudrate = config.get("baudrate", 115200)
            timeout = config.get("timeout", 1.0)

            conn = serial.Serial(interface, baudrate, timeout=timeout)
            logger.info("radio_connected", interface=interface, baudrate=baudrate)
            return conn
        except ImportError:
            logger.error("pyserial_not_available")
            raise
        except Exception as e:
            logger.error("radio_connect_failed", interface=interface, error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect radio modem."""
        try:
            if hasattr(connection_id, "close"):
                connection_id.close()
                logger.info("radio_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("radio_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data to radio modem."""
        try:
            if hasattr(connection_id, "write"):
                sent = connection_id.write(data)
                logger.debug("radio_sent", bytes=sent)
                return sent
            return 0
        except Exception as e:
            logger.error("radio_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from radio modem."""
        try:
            if hasattr(connection_id, "read"):
                data = connection_id.read(buffer_size)
                logger.debug("radio_received", bytes=len(data))
                return data
            return b""
        except Exception as e:
            logger.error("radio_receive_failed", error=str(e))
            return b""
