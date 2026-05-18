"""
Datacenter fabric transport adapter.
Detects SDN controllers and data center interconnect.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class DatacenterAdapter(BaseTransportAdapter):
    """Datacenter fabric adapter for SDN and spine-leaf."""

    family = "datacenter_fabric"
    display_name = "Datacenter Fabric (SDN/OpenFlow)"

    # SDN controller ports
    SDN_PORTS = {
        6633: "OpenFlow 1.0",
        6653: "OpenFlow 1.3+",
        8080: "SDN Controller REST API",
        9090: "SDN Management",
    }

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect datacenter fabric interfaces.

        Returns:
            List of detected SDN/fabric components
        """
        detected = []

        try:
            detected = await self._probe_sdn_ports()
        except Exception as e:
            logger.error("datacenter_detection_failed", error=str(e))

        logger.info("datacenter_detection_complete", count=len(detected))
        return detected

    async def _probe_sdn_ports(self) -> list[dict[str, Any]]:
        """Probe for SDN controller ports."""
        detected = []

        for port, protocol_name in self.SDN_PORTS.items():
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port),
                    timeout=0.5,
                )
                writer.close()
                await writer.wait_closed()

                detected.append(
                    {
                        "interface": f"sdn_{port}",
                        "port": port,
                        "protocol": protocol_name,
                        "status": "available",
                        "metadata": {
                            "medium": "network",
                            "protocol": protocol_name,
                            "datacenter": True,
                        },
                    }
                )
                logger.debug("datacenter_port_found", port=port, protocol=protocol_name)
            except (asyncio.TimeoutError, OSError):
                pass
            except Exception as e:
                logger.debug("datacenter_probe_failed", port=port)

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to datacenter controller."""
        try:
            host = config.get("host", "127.0.0.1")
            port = config.get("port", 6653)

            reader, writer = await asyncio.open_connection(host, port)
            logger.info("datacenter_connected", host=host, port=port)
            return (reader, writer)
        except Exception as e:
            logger.error("datacenter_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from datacenter controller."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.close()
                await writer.wait_closed()
                logger.info("datacenter_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("datacenter_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send command to datacenter."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.write(data)
                await writer.drain()
                logger.debug("datacenter_sent", bytes=len(data))
                return len(data)
            return 0
        except Exception as e:
            logger.error("datacenter_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive datacenter data."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                data = await asyncio.wait_for(reader.read(buffer_size), timeout=1.0)
                logger.debug("datacenter_received", bytes=len(data))
                return data
            return b""
        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            logger.error("datacenter_receive_failed", error=str(e))
            return b""
