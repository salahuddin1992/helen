"""
Specialty/vertical market adapter.
Detects DICOM, HL7, trading data, and GPU cluster protocols.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class SpecialtyAdapter(BaseTransportAdapter):
    """Specialty adapter for vertical market protocols."""

    family = "specialty_vertical"
    display_name = "Specialty/Vertical (DICOM/HL7/Trading/GPU)"

    # Specialty protocol ports
    SPECIALTY_PORTS = {
        104: "DICOM (medical imaging)",
        2575: "HL7 (healthcare)",
        9000: "Trading data feeds",
        16688: "NCCL (GPU clustering)",
    }

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect specialty protocol interfaces.

        Returns:
            List of detected specialty servers
        """
        detected = []

        try:
            detected = await self._probe_specialty_ports()
        except Exception as e:
            logger.error("specialty_detection_failed", error=str(e))

        logger.info("specialty_detection_complete", count=len(detected))
        return detected

    async def _probe_specialty_ports(self) -> list[dict[str, Any]]:
        """Probe for specialty protocol ports."""
        detected = []

        for port, protocol_name in self.SPECIALTY_PORTS.items():
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port),
                    timeout=0.5,
                )
                writer.close()
                await writer.wait_closed()

                detected.append(
                    {
                        "interface": f"specialty_{port}",
                        "port": port,
                        "protocol": protocol_name,
                        "status": "available",
                        "metadata": {
                            "medium": "network",
                            "protocol": protocol_name,
                        },
                    }
                )
                logger.debug("specialty_port_found", port=port, protocol=protocol_name)
            except (asyncio.TimeoutError, OSError):
                pass
            except Exception as e:
                logger.debug("specialty_port_probe_failed", port=port)

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to specialty service."""
        try:
            host = config.get("host", "127.0.0.1")
            port = config.get("port", 104)

            reader, writer = await asyncio.open_connection(host, port)
            logger.info("specialty_connected", host=host, port=port)
            return (reader, writer)
        except Exception as e:
            logger.error("specialty_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from specialty service."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.close()
                await writer.wait_closed()
                logger.info("specialty_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("specialty_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data to specialty service."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.write(data)
                await writer.drain()
                logger.debug("specialty_sent", bytes=len(data))
                return len(data)
            return 0
        except Exception as e:
            logger.error("specialty_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from specialty service."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                data = await asyncio.wait_for(reader.read(buffer_size), timeout=1.0)
                logger.debug("specialty_received", bytes=len(data))
                return data
            return b""
        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            logger.error("specialty_receive_failed", error=str(e))
            return b""
