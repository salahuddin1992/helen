"""
SCADA/utility infrastructure adapter.
Detects Modbus, DNP3, IEC 61850, and GOOSE protocols.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class ScadaAdapter(BaseTransportAdapter):
    """SCADA adapter for utility/infrastructure networks."""

    family = "scada_utility"
    display_name = "SCADA/Utility (Modbus/DNP3/IEC61850)"

    # SCADA protocol ports
    SCADA_PORTS = {
        502: "Modbus TCP",
        20000: "DNP3",
        102: "IEC 60870-5-104",
        2404: "IEC 60870-5-104",
    }

    # GOOSE multicast
    GOOSE_MULTICAST = "224.0.0.0"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect SCADA interfaces.

        Returns:
            List of detected SCADA servers
        """
        detected = []

        try:
            detected = await self._probe_scada_ports()
            detected.extend(await self._probe_goose())
        except Exception as e:
            logger.error("scada_detection_failed", error=str(e))

        logger.info("scada_detection_complete", count=len(detected))
        return detected

    async def _probe_scada_ports(self) -> list[dict[str, Any]]:
        """Probe for SCADA protocol ports."""
        detected = []

        for port, protocol_name in self.SCADA_PORTS.items():
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port),
                    timeout=0.5,
                )
                writer.close()
                await writer.wait_closed()

                detected.append(
                    {
                        "interface": f"scada_{port}",
                        "port": port,
                        "protocol": protocol_name,
                        "status": "available",
                        "metadata": {
                            "medium": "network",
                            "protocol": protocol_name,
                        },
                    }
                )
                logger.debug("scada_port_found", port=port, protocol=protocol_name)
            except (asyncio.TimeoutError, OSError):
                pass
            except Exception as e:
                logger.debug("scada_port_probe_failed", port=port)

        return detected

    async def _probe_goose(self) -> list[dict[str, Any]]:
        """Probe for IEC 61850 GOOSE multicast."""
        detected = []

        try:
            # Try to bind multicast socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                sock.bind(("", 3819))  # GOOSE port
                detected.append(
                    {
                        "interface": "goose_multicast",
                        "port": 3819,
                        "protocol": "GOOSE (IEC 61850)",
                        "status": "available",
                        "metadata": {
                            "medium": "network",
                            "protocol": "GOOSE",
                            "multicast": "224.0.0.0",
                        },
                    }
                )
            finally:
                sock.close()
        except Exception as e:
            logger.debug("goose_probe_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to SCADA server."""
        try:
            host = config.get("host", "127.0.0.1")
            port = config.get("port", 502)

            reader, writer = await asyncio.open_connection(host, port)
            logger.info("scada_connected", host=host, port=port)
            return (reader, writer)
        except Exception as e:
            logger.error("scada_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from SCADA server."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.close()
                await writer.wait_closed()
                logger.info("scada_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("scada_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send SCADA command."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.write(data)
                await writer.drain()
                logger.debug("scada_sent", bytes=len(data))
                return len(data)
            return 0
        except Exception as e:
            logger.error("scada_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive SCADA data."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                data = await asyncio.wait_for(reader.read(buffer_size), timeout=1.0)
                logger.debug("scada_received", bytes=len(data))
                return data
            return b""
        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            logger.error("scada_receive_failed", error=str(e))
            return b""
