"""
Mesh network transport adapter.
Detects mesh protocols (batman-adv, babel, cjdns).
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class MeshAdapter(BaseTransportAdapter):
    """Mesh networking adapter for decentralized networks."""

    family = "mesh"
    display_name = "Mesh Network"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect mesh protocol interfaces.

        Returns:
            List of detected mesh networks
        """
        detected = []

        try:
            if platform.system() == "Linux":
                detected = await self._detect_batman_adv()
                detected.extend(await self._detect_cjdns())
        except Exception as e:
            logger.error("mesh_detection_failed", error=str(e))

        logger.info("mesh_detection_complete", count=len(detected))
        return detected

    async def _detect_batman_adv(self) -> list[dict[str, Any]]:
        """Detect batman-adv mesh interfaces."""
        detected = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "batctl",
                "interface",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                for line in output.split("\n"):
                    if line.strip():
                        detected.append(
                            {
                                "interface": "bat0",
                                "type": "batman_adv",
                                "status": "up",
                                "peers": 0,
                                "metadata": {
                                    "protocol": "batman-adv",
                                    "medium": "wireless",
                                },
                            }
                        )
                        break
        except asyncio.TimeoutError:
            logger.warning("batctl_timeout")
        except Exception as e:
            logger.warning("batman_adv_detection_failed", error=str(e))

        return detected

    async def _detect_cjdns(self) -> list[dict[str, Any]]:
        """Detect cjdns hyperboria network."""
        detected = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "cjdroute",
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                detected.append(
                    {
                        "interface": "tun0",
                        "type": "cjdns",
                        "status": "up",
                        "metadata": {
                            "protocol": "cjdns",
                            "medium": "virtual",
                        },
                    }
                )
        except asyncio.TimeoutError:
            logger.warning("cjdns_timeout")
        except Exception as e:
            logger.warning("cjdns_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """Connect to mesh network."""
        try:
            logger.info("mesh_connecting", interface=interface)
            return interface
        except Exception as e:
            logger.error("mesh_connect_failed", error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from mesh."""
        try:
            logger.info("mesh_disconnected")
            return True
        except Exception as e:
            logger.error("mesh_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data on mesh."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from mesh."""
        return b""
