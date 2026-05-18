"""
Overlay tunnel transport adapter.
Detects tunnel interfaces (GRE, VXLAN, WireGuard).
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class OverlayTunnelAdapter(BaseTransportAdapter):
    """Overlay tunnel adapter for VPN and tunneling protocols."""

    family = "overlay_tunnel"
    display_name = "Overlay Tunnel (VPN/GRE/VXLAN)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect tunnel interfaces.

        Returns:
            List of detected tunnels
        """
        detected = []

        try:
            if platform.system() == "Linux":
                detected = await self._detect_linux_tunnels()
            elif platform.system() == "Windows":
                detected = await self._detect_windows_tunnels()
        except Exception as e:
            logger.error("tunnel_detection_failed", error=str(e))

        logger.info("tunnel_detection_complete", count=len(detected))
        return detected

    async def _detect_linux_tunnels(self) -> list[dict[str, Any]]:
        """Detect tunnel interfaces on Linux."""
        detected = []

        try:
            # Check tunnel interfaces
            proc = await asyncio.create_subprocess_exec(
                "ip",
                "tunnel",
                "show",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                for line in output.split("\n"):
                    if line.strip():
                        iface = line.split(":")[0].strip()
                        detected.append(
                            {
                                "interface": iface,
                                "type": "tunnel",
                                "status": "up",
                                "metadata": {"medium": "virtual", "protocol": "GRE"},
                            }
                        )

            # Check VXLAN interfaces
            proc = await asyncio.create_subprocess_exec(
                "ip",
                "link",
                "show",
                "type",
                "vxlan",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                for line in output.split("\n"):
                    if "vxlan" in line.lower():
                        detected.append(
                            {
                                "interface": "vxlan0",
                                "type": "vxlan",
                                "status": "up",
                                "metadata": {"medium": "virtual", "protocol": "VXLAN"},
                            }
                        )
                        break

            # Check WireGuard
            proc = await asyncio.create_subprocess_exec(
                "wg",
                "show",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                if output.strip():
                    detected.append(
                        {
                            "interface": "wg0",
                            "type": "wireguard",
                            "status": "up",
                            "metadata": {"medium": "virtual", "protocol": "WireGuard"},
                        }
                    )
        except Exception as e:
            logger.warning("linux_tunnel_detection_failed", error=str(e))

        return detected

    async def _detect_windows_tunnels(self) -> list[dict[str, Any]]:
        """Detect tunnel interfaces on Windows."""
        detected = []

        try:
            # Check for WireGuard service
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-Command",
                "Get-Service | Where-Object {$_.Name -like '*WireGuard*'}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                if "WireGuard" in output:
                    detected.append(
                        {
                            "interface": "wg-adapter",
                            "type": "wireguard",
                            "status": "available",
                            "metadata": {"medium": "virtual", "protocol": "WireGuard"},
                        }
                    )

            # Check for VPN adapters
            proc = await asyncio.create_subprocess_exec(
                "netsh",
                "ras",
                "show",
                "connection",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                detected.append(
                    {
                        "interface": "vpn",
                        "type": "vpn",
                        "status": "available",
                        "metadata": {"medium": "virtual", "protocol": "VPN"},
                    }
                )
        except Exception as e:
            logger.warning("windows_tunnel_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect tunnel."""
        try:
            logger.info("tunnel_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("tunnel_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect tunnel."""
        try:
            logger.info("tunnel_disconnected")
            return True
        except Exception as e:
            logger.error("tunnel_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data over tunnel."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from tunnel."""
        return b""
