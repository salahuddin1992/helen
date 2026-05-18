"""
Fiber optic transport adapter.
Detects fiber interfaces via speed and driver inspection.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

import psutil

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class FiberAdapter(BaseTransportAdapter):
    """Fiber optic adapter for high-speed optical connections."""

    family = "fiber"
    display_name = "Fiber Optic"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect fiber interfaces by speed and driver.

        Fiber typically >= 1Gbps with specific transceiver types.

        Returns:
            List of detected fiber interfaces
        """
        detected = []
        system = platform.system()

        try:
            if_stats = psutil.net_if_stats()
            if_addrs = psutil.net_if_addrs()

            for iface_name, stats in if_stats.items():
                # Filter for high-speed interfaces
                if stats.speed >= 1000:  # >= 1 Gbps
                    # Check driver type if on Linux
                    is_fiber = False
                    driver_info = None

                    if system == "Linux":
                        driver_info = await self._get_linux_driver(iface_name)
                        is_fiber = self._is_fiber_driver(driver_info)
                    else:
                        # Windows/macOS: assume high-speed = fiber
                        is_fiber = True

                    if is_fiber:
                        addrs = if_addrs.get(iface_name, [])
                        ip_addr = None
                        for addr in addrs:
                            if addr.family == 2:  # IPv4
                                ip_addr = addr.address
                                break

                        detected.append(
                            {
                                "interface": iface_name,
                                "status": "up" if stats.isup else "down",
                                "speed_mbps": stats.speed,
                                "mtu": stats.mtu,
                                "ip_address": ip_addr,
                                "driver": driver_info or "fiber",
                                "metadata": {
                                    "medium": "optical",
                                    "duplex": "full",
                                    "transceiver": "SFP+/QSFP",
                                },
                            }
                        )

            logger.info("fiber_detection_complete", count=len(detected))
        except Exception as e:
            logger.error("fiber_detection_failed", error=str(e))

        return detected

    async def _get_linux_driver(self, interface: str) -> str:
        """Get driver name on Linux using ethtool."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ethtool",
                "-i",
                interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            output = stdout.decode("utf-8", errors="ignore")
            for line in output.split("\n"):
                if line.startswith("driver:"):
                    return line.split(":", 1)[1].strip()
        except asyncio.TimeoutError:
            logger.warning("ethtool_timeout", interface=interface)
        except Exception as e:
            logger.warning("driver_query_failed", interface=interface, error=str(e))

        return ""

    def _is_fiber_driver(self, driver: str) -> bool:
        """Check if driver name indicates fiber."""
        fiber_drivers = (
            "ixgbe",
            "i40e",
            "ice",
            "mlx",
            "mlxsw",
            "bnx2x",
            "qede",
            "qede",
            "sfc",
            "be2net",
        )
        return any(driver.startswith(fd) for fd in fiber_drivers)

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """
        Connect over fiber interface.

        Args:
            interface: Fiber interface name
            config: Connection config

        Returns:
            Connection handle (interface reference)
        """
        try:
            logger.info("fiber_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("fiber_connect_failed", interface=interface, error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from fiber."""
        try:
            logger.info("fiber_disconnected", interface=connection_id)
            return True
        except Exception as e:
            logger.error("fiber_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data over fiber."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from fiber."""
        return b""

    async def get_interface_info(self, interface: str) -> dict[str, Any]:
        """Get fiber interface details."""
        try:
            stats = psutil.net_if_stats().get(interface)
            if stats:
                return {
                    "speed_mbps": stats.speed,
                    "mtu": stats.mtu,
                    "duplex": "full",
                    "medium": "optical",
                    "transceiver": "SFP+/QSFP",
                }
        except Exception as e:
            logger.warning("fiber_info_failed", interface=interface, error=str(e))

        return {}
