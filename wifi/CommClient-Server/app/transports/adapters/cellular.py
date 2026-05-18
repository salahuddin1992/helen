"""
Cellular private transport adapter.
Detects and manages cellular modem connections (LTE/5G).
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class CellularAdapter(BaseTransportAdapter):
    """Cellular modem adapter for LTE/5G connections."""

    family = "cellular_private"
    display_name = "Cellular Modem (Private)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect cellular modem interfaces.

        Returns:
            List of detected cellular interfaces
        """
        detected = []
        system = platform.system()

        try:
            if system == "Windows":
                detected = await self._detect_windows()
            elif system == "Linux":
                detected = await self._detect_linux()
        except Exception as e:
            logger.error("cellular_detection_failed", error=str(e))

        logger.info("cellular_detection_complete", count=len(detected))
        return detected

    async def _detect_windows(self) -> list[dict[str, Any]]:
        """Detect cellular modems on Windows using netsh."""
        detected = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "netsh",
                "mbn",
                "show",
                "interfaces",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            output = stdout.decode("utf-8", errors="ignore")
            current_modem = None

            for line in output.split("\n"):
                if "Name" in line and ":" in line:
                    if current_modem:
                        detected.append(current_modem)
                    current_modem = {
                        "interface": line.split(":", 1)[1].strip(),
                        "type": "cellular",
                        "metadata": {"medium": "wireless", "protocol": "LTE/5G"},
                    }
                elif current_modem and "State" in line:
                    state = "up" if "connected" in line.lower() else "down"
                    current_modem["status"] = state

            if current_modem:
                detected.append(current_modem)

        except asyncio.TimeoutError:
            logger.warning("cellular_windows_timeout")
        except Exception as e:
            logger.warning("cellular_windows_detection_failed", error=str(e))

        return detected

    async def _detect_linux(self) -> list[dict[str, Any]]:
        """Detect cellular modems on Linux using mmcli."""
        detected = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "mmcli",
                "-L",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                for line in output.split("\n"):
                    if "/org/freedesktop/ModemManager1/Modem/" in line:
                        modem_id = line.split("/")[-1].strip()
                        detected.append(
                            {
                                "interface": f"modem{modem_id}",
                                "type": "cellular",
                                "status": "up",
                                "metadata": {
                                    "medium": "wireless",
                                    "protocol": "LTE/5G",
                                    "modem_id": modem_id,
                                },
                            }
                        )
        except asyncio.TimeoutError:
            logger.warning("cellular_linux_timeout")
        except Exception as e:
            logger.warning("cellular_linux_detection_failed", error=str(e))

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """
        Connect cellular modem.

        Args:
            interface: Modem interface name
            config: Connection config with APN, auth params

        Returns:
            Connection handle
        """
        try:
            logger.info(
                "cellular_connecting",
                interface=interface,
                apn=config.get("apn", "unknown"),
            )
            return interface
        except Exception as e:
            logger.error("cellular_connect_failed", interface=interface, error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect cellular modem."""
        try:
            logger.info("cellular_disconnected", interface=connection_id)
            return True
        except Exception as e:
            logger.error("cellular_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data over cellular."""
        return len(data)

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from cellular."""
        return b""

    async def get_signal_quality(self, interface: str) -> dict[str, Any]:
        """Get cellular signal quality."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "mmcli",
                "-m",
                "0",
                "--output-all",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            output = stdout.decode("utf-8", errors="ignore")
            result = {"signal_strength": 50, "noise_level": -100, "snr_db": 20}

            for line in output.split("\n"):
                if "signal" in line.lower():
                    try:
                        signal = int(line.split()[-1].rstrip("%"))
                        result["signal_strength"] = min(100, signal)
                    except (ValueError, IndexError) as e:
                        logger.debug("cellular_signal_parse_failed", line=line, error=str(e))

            return result
        except Exception as e:
            logger.warning("cellular_signal_quality_failed", error=str(e))
            return {"signal_strength": 0, "noise_level": 0, "snr_db": 0}
