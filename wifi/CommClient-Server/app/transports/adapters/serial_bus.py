"""
Serial bus transport adapter.
Detects and manages serial buses (RS-485, CAN, SPI, I2C).
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class SerialBusAdapter(BaseTransportAdapter):
    """Serial bus adapter for RS-485, CAN, and other serial protocols."""

    family = "serial_bus"
    display_name = "Serial Bus (RS-485/CAN/SPI/I2C)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect serial bus interfaces (COM ports, USB devices).

        Returns:
            List of detected serial bus devices
        """
        detected = []

        try:
            import serial.tools.list_ports

            for port, desc, hwid in serial.tools.list_ports.comports():
                # Identify serial bus types
                bus_type = self._identify_bus_type(desc, hwid)
                if bus_type:
                    detected.append(
                        {
                            "interface": port,
                            "port": port,
                            "bus_type": bus_type,
                            "status": "available",
                            "description": desc,
                            "metadata": {
                                "medium": "serial",
                                "protocol": bus_type,
                                "hwid": hwid,
                            },
                        }
                    )

            logger.info("serial_bus_detection_complete", count=len(detected))
        except ImportError:
            logger.warning("pyserial_not_available")
        except Exception as e:
            logger.error("serial_bus_detection_failed", error=str(e))

        return detected

    def _identify_bus_type(self, description: str, hwid: str) -> str:
        """Identify serial bus type from description."""
        desc_lower = description.lower()
        hwid_lower = hwid.lower()

        if any(x in desc_lower for x in ["rs-485", "rs485", "485"]):
            return "RS-485"
        elif any(x in desc_lower for x in ["can", "can-bus"]):
            return "CAN-Bus"
        elif any(x in desc_lower for x in ["spi", "i2c", "i2s"]):
            return "SPI/I2C"
        elif "ft232" in hwid_lower or "ch340" in hwid_lower:
            return "RS-485"  # Common USB serial adapters

        return None

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """
        Connect to serial bus device.

        Args:
            interface: Serial port (e.g., COM3, /dev/ttyUSB0)
            config: Baudrate, timeout, etc.

        Returns:
            Serial connection handle
        """
        try:
            import serial

            baudrate = config.get("baudrate", 9600)
            timeout = config.get("timeout", 1.0)
            parity = config.get("parity", serial.PARITY_NONE)
            stopbits = config.get("stopbits", serial.STOPBITS_ONE)

            conn = serial.Serial(
                interface,
                baudrate=baudrate,
                timeout=timeout,
                parity=parity,
                stopbits=stopbits,
            )
            logger.info(
                "serial_bus_connected",
                interface=interface,
                baudrate=baudrate,
            )
            return conn
        except ImportError:
            logger.error("pyserial_not_available")
            raise
        except Exception as e:
            logger.error("serial_bus_connect_failed", interface=interface, error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from serial bus."""
        try:
            if hasattr(connection_id, "close"):
                connection_id.close()
                logger.info("serial_bus_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("serial_bus_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data to serial bus."""
        try:
            if hasattr(connection_id, "write"):
                sent = connection_id.write(data)
                logger.debug("serial_bus_sent", bytes=sent)
                return sent
            return 0
        except Exception as e:
            logger.error("serial_bus_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from serial bus."""
        try:
            if hasattr(connection_id, "read"):
                data = connection_id.read(buffer_size)
                logger.debug("serial_bus_received", bytes=len(data))
                return data
            return b""
        except Exception as e:
            logger.error("serial_bus_receive_failed", error=str(e))
            return b""
