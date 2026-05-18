"""
Industrial protocol transport adapter.
Detects industrial protocols (Modbus, EtherNet/IP, PROFINET, BACnet).
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class IndustrialAdapter(BaseTransportAdapter):
    """Industrial protocol adapter for factory/plant networks."""

    family = "industrial"
    display_name = "Industrial Protocols"

    # Known industrial protocol ports
    INDUSTRIAL_PORTS = {
        502: "Modbus TCP",
        44818: "EtherNet/IP",
        34962: "PROFINET",
        34963: "PROFINET",
        34964: "PROFINET",
        47808: "BACnet",
        5020: "EtherCAT",
        4840: "OPC UA",
        20000: "DNP3",
        102: "IEC 60870-5-104",
    }

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect industrial protocol interfaces via port scanning.

        Returns:
            List of detected industrial interfaces
        """
        detected = []

        try:
            detected = await self._probe_industrial_ports()
        except Exception as e:
            logger.error("industrial_detection_failed", error=str(e))

        logger.info("industrial_detection_complete", count=len(detected))
        return detected

    async def _probe_industrial_ports(self) -> list[dict[str, Any]]:
        """Probe localhost for industrial protocol ports — in parallel.

        Sequential probing of 10 ports at 0.5s each was 5s in the worst
        case, which timed out the smoke-test budget. ``asyncio.gather``
        collapses the worst case to ~0.5s.
        """

        async def probe(port: int, protocol_name: str):
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port),
                    timeout=0.5,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return {
                    "interface": f"industrial_{port}",
                    "port": port,
                    "protocol": protocol_name,
                    "status": "available",
                    "metadata": {
                        "medium": "network",
                        "protocol_family": "industrial",
                    },
                }
            except (asyncio.TimeoutError, OSError):
                return None
            except Exception as exc:
                logger.warning("industrial_port_probe_failed",
                               port=port, error=str(exc))
                return None

        results = await asyncio.gather(*(
            probe(p, n) for p, n in self.INDUSTRIAL_PORTS.items()
        ))
        return [r for r in results if r is not None]

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """
        Connect to industrial protocol server.

        Args:
            interface: Industrial interface identifier
            config: Config with 'host' and 'port'

        Returns:
            Connection handle (reader, writer tuple)
        """
        try:
            host = config.get("host", "127.0.0.1")
            port = config.get("port", 502)

            reader, writer = await asyncio.open_connection(host, port)
            logger.info("industrial_connected", host=host, port=port)
            return (reader, writer)
        except Exception as e:
            logger.error("industrial_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from industrial interface."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.close()
                await writer.wait_closed()
                logger.info("industrial_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("industrial_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data to industrial protocol server."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.write(data)
                await writer.drain()
                logger.debug("industrial_sent", bytes=len(data))
                return len(data)
            return 0
        except Exception as e:
            logger.error("industrial_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from industrial protocol server."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                data = await asyncio.wait_for(reader.read(buffer_size), timeout=1.0)
                logger.debug("industrial_received", bytes=len(data))
                return data
            return b""
        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            logger.error("industrial_receive_failed", error=str(e))
            return b""
