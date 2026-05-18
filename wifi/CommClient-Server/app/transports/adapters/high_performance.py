"""
High-performance transport adapter.
Detects InfiniBand, RoCE, and NVMe-oF interfaces.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class HighPerformanceAdapter(BaseTransportAdapter):
    """High-performance adapter for InfiniBand, RoCE, NVMe-oF."""

    family = "high_performance"
    display_name = "High-Performance (IB/RoCE/NVMe-oF)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect high-performance interfaces.

        Returns:
            List of detected HPC interfaces
        """
        detected = []

        if platform.system() == "Linux":
            detected.extend(await self._detect_infiniband())
            detected.extend(await self._detect_roce())
            detected.extend(await self._detect_nvme_of())

        logger.info("high_performance_detection_complete", count=len(detected))
        return detected

    async def _detect_infiniband(self) -> list[dict[str, Any]]:
        """Detect InfiniBand ports."""
        detected = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "ibstat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                port_num = 0

                for line in output.split("\n"):
                    if "CA " in line:
                        port_num += 1
                    if "Physical state:" in line and "LinkUp" in line:
                        detected.append(
                            {
                                "interface": f"ib{port_num}",
                                "type": "infiniband",
                                "status": "up",
                                "metadata": {
                                    "medium": "high_performance",
                                    "protocol": "InfiniBand",
                                },
                            }
                        )
        except asyncio.TimeoutError:
            logger.warning("ibstat_timeout")
        except Exception as e:
            logger.warning("infiniband_detection_failed", error=str(e))

        return detected

    async def _detect_roce(self) -> list[dict[str, Any]]:
        """Detect RoCE (RDMA over Converged Ethernet)."""
        detected = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "rdma",
                "link",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                for line in output.split("\n"):
                    if "link" in line.lower() and "ACTIVE" in line:
                        detected.append(
                            {
                                "interface": "roce0",
                                "type": "roce",
                                "status": "up",
                                "metadata": {
                                    "medium": "high_performance",
                                    "protocol": "RoCE",
                                },
                            }
                        )
                        break
        except asyncio.TimeoutError:
            logger.warning("rdma_timeout")
        except Exception as e:
            logger.warning("roce_detection_failed", error=str(e))

        return detected

    async def _detect_nvme_of(self) -> list[dict[str, Any]]:
        """Detect NVMe-oF connections."""
        detected = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "nvme",
                "list-subsys",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                if "nvme" in output:
                    detected.append(
                        {
                            "interface": "nvme0",
                            "type": "nvme_of",
                            "status": "up",
                            "metadata": {
                                "medium": "high_performance",
                                "protocol": "NVMe-oF",
                            },
                        }
                    )
        except asyncio.TimeoutError:
            logger.warning("nvme_timeout")
        except Exception as e:
            logger.warning("nvme_of_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to high-performance interface."""
        try:
            logger.info("high_performance_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("high_performance_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from HPC interface."""
        try:
            logger.info("high_performance_disconnected")
            return True
        except Exception as e:
            logger.error("high_performance_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data over HPC."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from HPC."""
        return b""
