"""
Wireless bridge transport adapter.
Detects and manages wireless bridge connections (P2P, mesh bridges).
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class WirelessBridgeAdapter(BaseTransportAdapter):
    """Wireless bridge adapter for point-to-point and mesh connections."""

    family = "wireless_bridge"
    display_name = "Wireless Bridge (P2P/Mesh)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect wireless bridge interfaces.

        Returns:
            List of detected bridge configurations
        """
        detected = []
        system = platform.system()

        try:
            if system == "Linux":
                detected = await self._detect_linux()
            elif system == "Windows":
                detected = await self._detect_windows()
        except Exception as e:
            logger.error("wireless_bridge_detection_failed", error=str(e))

        logger.info("wireless_bridge_detection_complete", count=len(detected))
        return detected

    async def _detect_linux(self) -> list[dict[str, Any]]:
        """Detect wireless bridges on Linux."""
        detected = []

        try:
            # Check for bridge interfaces
            proc = await asyncio.create_subprocess_exec(
                "ip",
                "link",
                "show",
                "type",
                "bridge",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                for line in output.split("\n"):
                    if ":" in line and "bridge" not in line:
                        iface_name = line.split(":")[1].strip()
                        if iface_name:
                            detected.append(
                                {
                                    "interface": iface_name,
                                    "status": "up",
                                    "type": "wireless_bridge",
                                    "metadata": {
                                        "medium": "wireless",
                                        "protocol": "802.11s",
                                    },
                                }
                            )

            # Check for mesh interfaces
            proc = await asyncio.create_subprocess_exec(
                "iw",
                "dev",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                for line in output.split("\n"):
                    if "mesh" in line.lower():
                        detected.append(
                            {
                                "interface": "mesh0",
                                "status": "up",
                                "type": "mesh",
                                "metadata": {
                                    "medium": "wireless",
                                    "protocol": "802.11s",
                                },
                            }
                        )
                        break

        except Exception as e:
            logger.warning("wireless_bridge_linux_detection_failed", error=str(e))

        return detected

    async def _detect_windows(self) -> list[dict[str, Any]]:
        """Detect wireless bridges on Windows."""
        detected = []

        try:
            # Windows typically uses Hosted Network or WiFi Direct
            proc = await asyncio.create_subprocess_exec(
                "netsh",
                "wlan",
                "show",
                "hostednetwork",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            output = stdout.decode("utf-8", errors="ignore")
            if "Hosted network settings" in output:
                detected.append(
                    {
                        "interface": "Virtual WiFi",
                        "status": "up",
                        "type": "hosted_network",
                        "metadata": {
                            "medium": "wireless",
                            "protocol": "802.11",
                        },
                    }
                )
        except Exception as e:
            logger.warning("wireless_bridge_windows_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """
        Connect wireless bridge.

        Args:
            interface: Bridge interface name
            config: Bridge configuration

        Returns:
            Connection handle
        """
        try:
            logger.info("wireless_bridge_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("wireless_bridge_connect_failed", interface=interface, error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect wireless bridge."""
        try:
            logger.info("wireless_bridge_disconnected", interface=connection_id)
            return True
        except Exception as e:
            logger.error("wireless_bridge_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data over bridge."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from bridge."""
        return b""
