"""
Management interface adapter.
Detects IPMI, BMC, and Redfish out-of-band management.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class ManagementAdapter(BaseTransportAdapter):
    """Management interface adapter for IPMI and Redfish."""

    family = "management"
    display_name = "Management Interface (IPMI/Redfish)"

    # Known management interface ports
    MGMT_PORTS = {
        623: "IPMI (RMCP)",
        5900: "VNC (IPMI)",
        443: "Redfish (HTTPS)",
        8443: "Redfish (HTTPS)",
    }

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect management interfaces.

        Returns:
            List of detected management ports
        """
        detected = []

        try:
            detected = await self._probe_management_ports()
            detected.extend(await self._probe_redfish())
        except Exception as e:
            logger.error("management_detection_failed", error=str(e))

        logger.info("management_detection_complete", count=len(detected))
        return detected

    async def _probe_management_ports(self) -> list[dict[str, Any]]:
        """Probe for management ports — in parallel."""

        async def probe(port: int, svc: str):
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
                    "interface": f"mgmt_{port}",
                    "port": port,
                    "service": svc,
                    "status": "available",
                    "metadata": {"medium": "network", "protocol": svc},
                }
            except (asyncio.TimeoutError, OSError):
                return None
            except Exception as exc:
                logger.debug("management_probe_failed",
                             port=port, error=str(exc))
                return None

        results = await asyncio.gather(*(
            probe(p, s) for p, s in self.MGMT_PORTS.items()
        ))
        return [r for r in results if r is not None]

    async def _probe_redfish(self) -> list[dict[str, Any]]:
        """Probe for Redfish endpoints."""
        detected = []

        redfish_hosts = ["127.0.0.1", "localhost"]

        for host in redfish_hosts:
            for port in [443, 8443]:
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port),
                        timeout=0.5,
                    )
                    writer.close()
                    await writer.wait_closed()

                    detected.append(
                        {
                            "interface": f"redfish_{host}_{port}",
                            "host": host,
                            "port": port,
                            "protocol": "Redfish",
                            "status": "available",
                            "metadata": {
                                "medium": "network",
                                "protocol": "Redfish (REST API)",
                            },
                        }
                    )
                except (asyncio.TimeoutError, OSError):
                    pass
                except Exception as e:
                    logger.debug("redfish_probe_failed", host=host, port=port)

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to management interface."""
        try:
            host = config.get("host", "127.0.0.1")
            port = config.get("port", 623)

            reader, writer = await asyncio.open_connection(host, port)
            logger.info("management_connected", host=host, port=port)
            return (reader, writer)
        except Exception as e:
            logger.error("management_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from management interface."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.close()
                await writer.wait_closed()
                logger.info("management_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("management_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send management command."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.write(data)
                await writer.drain()
                logger.debug("management_sent", bytes=len(data))
                return len(data)
            return 0
        except Exception as e:
            logger.error("management_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive management response."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                data = await asyncio.wait_for(reader.read(buffer_size), timeout=1.0)
                logger.debug("management_received", bytes=len(data))
                return data
            return b""
        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            logger.error("management_receive_failed", error=str(e))
            return b""
