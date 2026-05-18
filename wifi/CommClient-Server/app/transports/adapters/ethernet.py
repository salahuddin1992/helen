"""
Ethernet transport adapter.
Detects and manages wired Ethernet connections.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import psutil

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class EthernetAdapter(BaseTransportAdapter):
    """Ethernet adapter using system network interfaces."""

    family = "ethernet"
    display_name = "Ethernet (Wired)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect Ethernet interfaces using psutil.

        Returns:
            List of detected Ethernet interfaces
        """
        detected = []
        try:
            # Get all network interfaces
            if_addrs = psutil.net_if_addrs()
            if_stats = psutil.net_if_stats()

            for iface_name, addrs in if_addrs.items():
                # Filter for wired interfaces (typically eth*, en* on Unix, skip lo, virtual)
                if self._is_ethernet_interface(iface_name):
                    try:
                        stats = if_stats.get(iface_name)
                        if stats:
                            ipv4_addr = None
                            for addr in addrs:
                                if addr.family == socket.AF_INET:
                                    ipv4_addr = addr.address
                                    break

                            detected.append(
                                {
                                    "interface": iface_name,
                                    "status": "up" if stats.isup else "down",
                                    "speed_mbps": stats.speed,
                                    "mtu": stats.mtu,
                                    "is_up": stats.isup,
                                    "ip_address": ipv4_addr,
                                    "metadata": {
                                        "driver": "ethernet",
                                        "medium": "wired",
                                    },
                                }
                            )
                    except Exception as e:
                        logger.warning(
                            "ethernet_interface_error",
                            interface=iface_name,
                            error=str(e),
                        )

            logger.info("ethernet_detection_complete", count=len(detected))
        except Exception as e:
            logger.error("ethernet_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """
        Create socket connection on Ethernet interface.

        Args:
            interface: Interface name (e.g., 'eth0')
            config: Connection config with 'port' and optional 'protocol' ('tcp'/'udp')

        Returns:
            Socket object bound to interface
        """
        try:
            protocol = config.get("protocol", "tcp").lower()
            port = config.get("port", 0)

            if protocol == "tcp":
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            # Bind to interface if specified
            if interface and interface != "any":
                try:
                    # Get IP of interface
                    addrs = psutil.net_if_addrs().get(interface, [])
                    for addr in addrs:
                        if addr.family == socket.AF_INET:
                            sock.bind((addr.address, port))
                            break
                except Exception as e:
                    logger.warning("ethernet_bind_failed", interface=interface, error=str(e))
                    sock.bind(("0.0.0.0", port))
            else:
                sock.bind(("0.0.0.0", port))

            logger.info(
                "ethernet_connected",
                interface=interface,
                protocol=protocol,
                port=port,
            )
            return sock
        except Exception as e:
            logger.error(
                "ethernet_connect_failed",
                interface=interface,
                error=str(e),
            )
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect socket (connection_id is socket reference)."""
        try:
            if hasattr(connection_id, "close"):
                connection_id.close()
                logger.info("ethernet_disconnected")
                return True
            return False
        except Exception as e:
            logger.error("ethernet_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data on socket."""
        try:
            if hasattr(connection_id, "send"):
                sent = connection_id.send(data)
                logger.debug("ethernet_sent", bytes=sent)
                return sent
            return 0
        except Exception as e:
            logger.error("ethernet_send_failed", error=str(e))
            return 0

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from socket."""
        try:
            if hasattr(connection_id, "recv"):
                data = connection_id.recv(buffer_size)
                logger.debug("ethernet_received", bytes=len(data))
                return data
            return b""
        except Exception as e:
            logger.error("ethernet_receive_failed", error=str(e))
            return b""

    async def get_interface_info(self, interface: str) -> dict[str, Any]:
        """Get Ethernet interface details."""
        try:
            stats = psutil.net_if_stats().get(interface)
            if stats:
                return {
                    "speed_mbps": stats.speed,
                    "mtu": stats.mtu,
                    "duplex": "full",  # Ethernet is typically full-duplex
                    "driver": "ethernet",
                    "up": stats.isup,
                }
        except Exception as e:
            logger.warning("ethernet_info_failed", interface=interface, error=str(e))

        return {}

    def _is_ethernet_interface(self, name: str) -> bool:
        """Check if interface name matches Ethernet pattern."""
        # Unix/Linux: eth*, en*
        # Windows: Ethernet*
        # Exclude loopback, virtual, docker
        exclude_patterns = ("lo", "docker", "veth", "br-", "vlan")
        if any(name.startswith(p) for p in exclude_patterns):
            return False

        return name.startswith(("eth", "en", "Ethernet")) or (
            name.startswith("e") and len(name) > 1 and name[1:3].isdigit()
        )
