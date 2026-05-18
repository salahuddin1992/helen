"""
Audio/Video network transport adapter.
Detects professional AV protocols (Dante, AES67, NDI).
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class AVNetworkAdapter(BaseTransportAdapter):
    """AV network adapter for professional audio/video protocols."""

    family = "av_network"
    display_name = "AV Network (Dante/AES67/NDI)"

    # Known AV protocol ports
    AV_PORTS = {
        4440: "Dante (control)",
        8700: "Dante (audio)",
        8701: "Dante (audio)",
        8702: "Dante (audio)",
        5960: "NDI discovery",
        5961: "NDI",
    }

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect AV network interfaces via port scanning and mDNS.

        Returns:
            List of detected AV devices
        """
        detected = []

        try:
            detected = await self._probe_av_ports()
            detected.extend(await self._probe_mdns_av_devices())
        except Exception as e:
            logger.error("av_network_detection_failed", error=str(e))

        logger.info("av_network_detection_complete", count=len(detected))
        return detected

    async def _probe_av_ports(self) -> list[dict[str, Any]]:
        """Probe for AV protocol ports — in parallel."""

        async def probe(port: int, proto: str):
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
                    "interface": f"av_{port}",
                    "port": port,
                    "protocol": proto,
                    "status": "available",
                    "metadata": {
                        "medium": "network",
                        "protocol_family": "av_network",
                    },
                }
            except (asyncio.TimeoutError, OSError):
                return None
            except Exception as exc:
                logger.debug("av_port_probe_failed",
                             port=port, error=str(exc))
                return None

        results = await asyncio.gather(*(
            probe(p, n) for p, n in self.AV_PORTS.items()
        ))
        return [r for r in results if r is not None]

    async def _probe_mdns_av_devices(self) -> list[dict[str, Any]]:
        """Probe for AV devices via mDNS."""
        detected = []

        try:
            import zeroconf

            mdns = zeroconf.Zeroconf()

            # Common AV service types
            service_types = [
                "_dante._tcp.local.",
                "_aes67._udp.local.",
                "_ndi._tcp.local.",
            ]

            for service_type in service_types:
                try:
                    services = zeroconf.ServiceBrowser(mdns, service_type, handlers=[])
                    # Give brief time to discover
                    await asyncio.sleep(0.5)

                except Exception as e:
                    logger.debug("mdns_service_error", service=service_type, error=str(e))

            mdns.close()
        except ImportError:
            logger.debug("zeroconf_not_available")
        except Exception as e:
            logger.warning("mdns_av_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to AV device."""
        try:
            host = config.get("host", "127.0.0.1")
            port = config.get("port", 4440)

            reader, writer = await asyncio.open_connection(host, port)
            logger.info("av_network_connected", host=host, port=port)
            return (reader, writer)
        except Exception as e:
            logger.error("av_network_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from AV device."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.close()
                await writer.wait_closed()
                logger.info("av_network_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("av_network_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data to AV device."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                writer.write(data)
                await writer.drain()
                logger.debug("av_network_sent", bytes=len(data))
                return len(data)
            return 0
        except Exception as e:
            logger.error("av_network_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from AV device."""
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, writer = connection_id
                data = await asyncio.wait_for(reader.read(buffer_size), timeout=1.0)
                logger.debug("av_network_received", bytes=len(data))
                return data
            return b""
        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            logger.error("av_network_receive_failed", error=str(e))
            return b""
