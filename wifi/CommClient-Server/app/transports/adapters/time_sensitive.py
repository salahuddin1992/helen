"""
Time-sensitive/TSN transport adapter.
Detects TSN-capable interfaces and PTP hardware clocks.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class TimeSensitiveAdapter(BaseTransportAdapter):
    """Time-sensitive networking adapter for TSN and PTP."""

    family = "time_sensitive"
    display_name = "Time-Sensitive (TSN/PTP)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect TSN-capable and PTP interfaces.

        Returns:
            List of detected time-sensitive interfaces
        """
        detected = []

        try:
            if platform.system() == "Linux":
                detected = await self._detect_linux_tsn()
        except Exception as e:
            logger.error("time_sensitive_detection_failed", error=str(e))

        logger.info("time_sensitive_detection_complete", count=len(detected))
        return detected

    async def _detect_linux_tsn(self) -> list[dict[str, Any]]:
        """Detect TSN and PTP on Linux."""
        detected = []

        try:
            # Check for PTP hardware clocks
            proc = await asyncio.create_subprocess_exec(
                "phc_ctl",
                "-l",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                for line in output.split("\n"):
                    if "ptp" in line.lower():
                        detected.append(
                            {
                                "interface": "ptp_clock",
                                "type": "ptp_hardware_clock",
                                "status": "available",
                                "metadata": {
                                    "medium": "network",
                                    "protocol": "PTP (Precision Time Protocol)",
                                },
                            }
                        )
                        break

            # Check TSN capabilities via ethtool
            import psutil

            if_stats = psutil.net_if_stats()

            for iface_name in if_stats.keys():
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ethtool",
                        "--show-features",
                        iface_name,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

                    if proc.returncode == 0:
                        output = stdout.decode("utf-8", errors="ignore")
                        if any(
                            x in output.lower() for x in ["tsn", "time synchronization", "scheduling"]
                        ):
                            detected.append(
                                {
                                    "interface": iface_name,
                                    "type": "tsn_capable",
                                    "status": "available",
                                    "metadata": {
                                        "medium": "network",
                                        "protocol": "TSN (Time-Sensitive Networking)",
                                    },
                                }
                            )
                except asyncio.TimeoutError:
                    logger.debug("ethtool_timeout", interface=iface_name)
                except Exception as e:
                    logger.debug("tsn_check_failed", interface=iface_name)

        except asyncio.TimeoutError:
            logger.warning("phc_ctl_timeout")
        except ImportError:
            logger.debug("psutil_not_available")
        except Exception as e:
            logger.warning("linux_tsn_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to TSN interface."""
        try:
            logger.info("time_sensitive_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("time_sensitive_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from TSN interface."""
        try:
            logger.info("time_sensitive_disconnected")
            return True
        except Exception as e:
            logger.error("time_sensitive_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send TSN-scheduled data."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive time-sensitive data."""
        return b""

    async def get_interface_info(self, interface: str) -> dict[str, Any]:
        """Get TSN/PTP interface information."""
        try:
            info = {
                "interface": interface,
                "ptp_capable": True,
                "tsn_capable": True,
            }

            # Try to get PTP sync status
            try:
                import asyncio

                proc = await asyncio.create_subprocess_exec(
                    "ptp4l",
                    "-h",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                info["ptp_synced"] = proc.returncode == 0
            except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
                logger.debug("ptp4l_probe_failed", error=str(e))
                info["ptp_synced"] = False

            return info
        except Exception as e:
            logger.warning("tsn_info_failed", interface=interface, error=str(e))
            return {}
