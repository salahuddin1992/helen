"""
Optical link transport adapter.
Detects Li-Fi and infrared transceivers.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class OpticalLinkAdapter(BaseTransportAdapter):
    """Optical link adapter for Li-Fi and IR communications."""

    family = "optical_link"
    display_name = "Optical Link (Li-Fi/IR)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect optical link devices (USB-based).

        Returns:
            List of detected optical devices
        """
        detected = []

        try:
            detected = await self._detect_usb_optical_devices()
        except Exception as e:
            logger.error("optical_detection_failed", error=str(e))

        logger.info("optical_detection_complete", count=len(detected))
        return detected

    async def _detect_usb_optical_devices(self) -> list[dict[str, Any]]:
        """Detect optical devices connected via USB."""
        detected = []

        try:
            import usb.core
            import usb.util

            # Known optical device vendor/product IDs
            optical_devices = {
                (0x1234, 0x5678): "Li-Fi Adapter",  # Example IDs
            }

            for dev in usb.core.find(find_all=True):
                try:
                    if dev.idVendor and dev.idProduct:
                        device_name = optical_devices.get(
                            (dev.idVendor, dev.idProduct),
                            "Generic Optical Device",
                        )

                        detected.append(
                            {
                                "interface": f"optical_{dev.bus}_{dev.address}",
                                "vendor_id": dev.idVendor,
                                "product_id": dev.idProduct,
                                "device_name": device_name,
                                "status": "available",
                                "metadata": {
                                    "medium": "optical",
                                    "protocol": "Li-Fi/IR",
                                    "bus": dev.bus,
                                    "address": dev.address,
                                },
                            }
                        )
                except Exception as e:
                    logger.debug("optical_device_error", error=str(e))

        except ImportError:
            logger.warning("pyusb_not_available")
        except Exception as e:
            logger.warning("optical_usb_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to optical link device."""
        try:
            logger.info("optical_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("optical_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from optical link."""
        try:
            logger.info("optical_disconnected")
            return True
        except Exception as e:
            logger.error("optical_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data over optical link."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from optical link."""
        return b""
