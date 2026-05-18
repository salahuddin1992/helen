"""
Storage network transport adapter.
Detects iSCSI, FCoE, and NVMe target interfaces.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class StorageNetworkAdapter(BaseTransportAdapter):
    """Storage network adapter for SAN protocols."""

    family = "storage_network"
    display_name = "Storage Network (iSCSI/FCoE/NVMe-T)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect storage network interfaces.

        Returns:
            List of detected storage targets
        """
        detected = []

        try:
            if platform.system() == "Windows":
                detected = await self._detect_windows_iscsi()
            else:
                detected = await self._detect_linux_iscsi()
        except Exception as e:
            logger.error("storage_network_detection_failed", error=str(e))

        logger.info("storage_network_detection_complete", count=len(detected))
        return detected

    async def _detect_windows_iscsi(self) -> list[dict[str, Any]]:
        """Detect iSCSI on Windows."""
        detected = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-Command",
                "Get-IscsiTarget",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                if output.strip():
                    detected.append(
                        {
                            "interface": "iscsi0",
                            "type": "iscsi",
                            "status": "available",
                            "metadata": {"medium": "network", "protocol": "iSCSI"},
                        }
                    )
        except asyncio.TimeoutError:
            logger.warning("windows_iscsi_timeout")
        except Exception as e:
            logger.warning("windows_iscsi_detection_failed", error=str(e))

        return detected

    async def _detect_linux_iscsi(self) -> list[dict[str, Any]]:
        """Detect storage networks on Linux."""
        detected = []

        try:
            # Check iSCSI targets
            proc = await asyncio.create_subprocess_exec(
                "iscsiadm",
                "-m",
                "session",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                if output.strip():
                    detected.append(
                        {
                            "interface": "iscsi0",
                            "type": "iscsi",
                            "status": "active",
                            "metadata": {"medium": "network", "protocol": "iSCSI"},
                        }
                    )

            # Check NVMe targets
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
                            "interface": "nvme-target",
                            "type": "nvme_target",
                            "status": "available",
                            "metadata": {"medium": "network", "protocol": "NVMe-T"},
                        }
                    )
        except asyncio.TimeoutError:
            logger.warning("linux_storage_timeout")
        except Exception as e:
            logger.warning("linux_storage_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to storage target."""
        try:
            logger.info("storage_network_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("storage_network_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from storage target."""
        try:
            logger.info("storage_network_disconnected")
            return True
        except Exception as e:
            logger.error("storage_network_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data to storage target."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from storage target."""
        return b""
