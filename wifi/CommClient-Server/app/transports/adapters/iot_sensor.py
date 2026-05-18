"""
IoT sensor transport adapter.
Detects Zigbee, Z-Wave, Thread, and Bluetooth devices.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class IoTSensorAdapter(BaseTransportAdapter):
    """IoT sensor adapter for Zigbee, Z-Wave, Thread, Bluetooth."""

    family = "iot_sensor"
    display_name = "IoT Sensors (Zigbee/Z-Wave/BLE/Thread)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect IoT sensor devices.

        Returns:
            List of detected IoT devices
        """
        detected = []

        try:
            detected = await self._detect_serial_iot_devices()
            detected.extend(await self._detect_bluetooth_adapters())
        except Exception as e:
            logger.error("iot_sensor_detection_failed", error=str(e))

        logger.info("iot_sensor_detection_complete", count=len(detected))
        return detected

    async def _detect_serial_iot_devices(self) -> list[dict[str, Any]]:
        """Detect Zigbee/Z-Wave coordinators on serial ports."""
        detected = []

        try:
            import serial.tools.list_ports

            iot_keywords = ["zigbee", "z-wave", "zwave", "thread", "ieee 802.15.4"]

            for port, desc, hwid in serial.tools.list_ports.comports():
                if any(kw in desc.lower() for kw in iot_keywords):
                    device_type = "unknown"
                    if "zigbee" in desc.lower():
                        device_type = "zigbee"
                    elif "z-wave" in desc.lower() or "zwave" in desc.lower():
                        device_type = "zwave"
                    elif "thread" in desc.lower():
                        device_type = "thread"

                    detected.append(
                        {
                            "interface": port,
                            "port": port,
                            "device_type": device_type,
                            "description": desc,
                            "status": "available",
                            "metadata": {
                                "medium": "wireless",
                                "protocol": device_type.upper(),
                            },
                        }
                    )

        except ImportError:
            logger.debug("pyserial_not_available")
        except Exception as e:
            logger.warning("iot_serial_detection_failed", error=str(e))

        return detected

    async def _detect_bluetooth_adapters(self) -> list[dict[str, Any]]:
        """Detect Bluetooth adapters."""
        detected = []

        try:
            import bluetooth

            # Get available Bluetooth devices
            devices = bluetooth.discover_devices(lookup_names=True, duration=2)

            if devices:
                detected.append(
                    {
                        "interface": "hci0",
                        "type": "bluetooth",
                        "device_count": len(devices),
                        "status": "available",
                        "devices": [{"address": addr, "name": name} for addr, name in devices],
                        "metadata": {
                            "medium": "wireless",
                            "protocol": "Bluetooth",
                        },
                    }
                )
        except ImportError:
            logger.debug("pybluez_not_available")
        except Exception as e:
            logger.warning("bluetooth_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to IoT device."""
        try:
            device_type = config.get("device_type", "zigbee")
            logger.info("iot_sensor_connecting", interface=interface, type=device_type)
            return interface
        except Exception as e:
            logger.error("iot_sensor_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from IoT device."""
        try:
            logger.info("iot_sensor_disconnected")
            return True
        except Exception as e:
            logger.error("iot_sensor_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data to IoT device."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from IoT device."""
        return b""
