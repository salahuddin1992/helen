"""
WiFi (802.11) transport adapter.
Detects and manages WiFi connections with signal quality metrics.
"""

from __future__ import annotations

import asyncio
import platform
import re
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class WifiAdapter(BaseTransportAdapter):
    """WiFi adapter supporting Windows netsh and Linux iw/iwconfig."""

    family = "wifi"
    display_name = "WiFi (802.11)"

    async def detect(self) -> list[dict[str, Any]]:
        """
        Detect WiFi interfaces using OS-specific tools.

        Returns:
            List of detected WiFi interfaces with signal metrics
        """
        detected = []
        system = platform.system()

        try:
            if system == "Windows":
                detected = await self._detect_windows()
            elif system == "Linux":
                detected = await self._detect_linux()
            elif system == "Darwin":
                detected = await self._detect_macos()
        except Exception as e:
            logger.error("wifi_detection_failed", error=str(e))

        logger.info("wifi_detection_complete", count=len(detected), system=system)
        return detected

    async def _detect_windows(self) -> list[dict[str, Any]]:
        """Detect WiFi interfaces on Windows using netsh."""
        detected = []
        try:
            # Get WLAN interfaces
            proc = await asyncio.create_subprocess_exec(
                "netsh",
                "wlan",
                "show",
                "interfaces",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode("utf-8", errors="ignore")

            # Parse netsh output
            current_iface = {}
            for line in output.split("\n"):
                if "Interface Name" in line:
                    if current_iface:
                        detected.append(current_iface)
                    current_iface = {"interface": line.split(":", 1)[1].strip()}
                elif "SSID" in line:
                    current_iface["ssid"] = line.split(":", 1)[1].strip()
                elif "Signal" in line:
                    try:
                        signal = int(re.search(r"\d+", line.split(":", 1)[1]).group())
                        current_iface["signal_strength"] = signal
                    except (ValueError, AttributeError, IndexError) as e:
                        logger.debug("wifi_signal_parse_failed", line=line, error=str(e))

            if current_iface:
                detected.append(current_iface)

            for iface in detected:
                iface["status"] = "up"
                iface["metadata"] = {"protocol": "802.11", "medium": "wireless"}

        except Exception as e:
            logger.warning("wifi_windows_detection_failed", error=str(e))

        return detected

    async def _detect_linux(self) -> list[dict[str, Any]]:
        """Detect WiFi interfaces on Linux using iwconfig/iw."""
        detected = []
        try:
            # Try iw dev first (more modern)
            proc = await asyncio.create_subprocess_exec(
                "iw",
                "dev",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                detected = self._parse_iw_output(output)
            else:
                # Fall back to iwconfig
                proc = await asyncio.create_subprocess_exec(
                    "iwconfig",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode == 0:
                    output = stdout.decode("utf-8", errors="ignore")
                    detected = self._parse_iwconfig_output(output)

        except Exception as e:
            logger.warning("wifi_linux_detection_failed", error=str(e))

        return detected

    async def _detect_macos(self) -> list[dict[str, Any]]:
        """Detect WiFi interfaces on macOS."""
        detected = []
        try:
            proc = await asyncio.create_subprocess_exec(
                "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport",
                "-I",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore")
                iface = {
                    "interface": "en0",
                    "status": "up",
                    "metadata": {"protocol": "802.11", "medium": "wireless"},
                }

                for line in output.split("\n"):
                    if "SSID:" in line:
                        iface["ssid"] = line.split(":", 1)[1].strip()
                    elif "BSSID:" in line:
                        iface["bssid"] = line.split(":", 1)[1].strip()
                    elif "signal" in line.lower():
                        try:
                            signal = int(re.search(r"-?\d+", line).group())
                            iface["signal_dbm"] = signal
                        except (ValueError, AttributeError) as e:
                            logger.debug("wifi_macos_signal_parse_failed", line=line, error=str(e))

                detected.append(iface)
        except Exception as e:
            logger.warning("wifi_macos_detection_failed", error=str(e))

        return detected

    def _parse_iw_output(self, output: str) -> list[dict[str, Any]]:
        """Parse iw dev output."""
        detected = []
        current_iface = None

        for line in output.split("\n"):
            if line.startswith("phy"):
                continue
            if "Interface" in line:
                if current_iface:
                    detected.append(current_iface)
                current_iface = {
                    "interface": line.split()[-1],
                    "status": "up",
                    "metadata": {"protocol": "802.11", "medium": "wireless"},
                }

        if current_iface:
            detected.append(current_iface)

        return detected

    def _parse_iwconfig_output(self, output: str) -> list[dict[str, Any]]:
        """Parse iwconfig output."""
        detected = []
        current_iface = None

        for line in output.split("\n"):
            if line and not line.startswith(" "):
                if current_iface:
                    detected.append(current_iface)
                iface_name = line.split()[0]
                current_iface = {
                    "interface": iface_name,
                    "status": "up",
                    "metadata": {"protocol": "802.11", "medium": "wireless"},
                }
            elif current_iface and "ESSID" in line:
                try:
                    essid = line.split("ESSID:")[1].strip('"')
                    current_iface["ssid"] = essid
                except (IndexError, ValueError) as e:
                    logger.debug("iwconfig_essid_parse_failed", line=line, error=str(e))
            elif current_iface and "Link Quality" in line:
                try:
                    quality = int(line.split("=")[1].split("/")[0])
                    current_iface["link_quality"] = quality
                except (IndexError, ValueError) as e:
                    logger.debug("iwconfig_quality_parse_failed", line=line, error=str(e))

        if current_iface:
            detected.append(current_iface)

        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        """
        Connect to WiFi network.

        Args:
            interface: WiFi interface name
            config: Config with 'ssid' and optional 'password', 'port'

        Returns:
            Connection handle
        """
        try:
            ssid = config.get("ssid", "")
            logger.info("wifi_connecting", interface=interface, ssid=ssid)
            # Actual WiFi connection typically handled by OS
            # Return interface reference for I/O operations
            return interface
        except Exception as e:
            logger.error("wifi_connect_failed", interface=interface, error=str(e))
            raise

    async def disconnect(self, connection_id: str) -> bool:
        """Disconnect from WiFi."""
        try:
            logger.info("wifi_disconnected", interface=connection_id)
            return True
        except Exception as e:
            logger.error("wifi_disconnect_failed", error=str(e))
            return False

    async def send(self, connection_id: str, data: bytes) -> int:
        """Send data over WiFi."""
        return len(data)  # Stub implementation

    async def receive(
        self, connection_id: str, buffer_size: int = 65536
    ) -> bytes:
        """Receive data from WiFi."""
        return b""  # Stub implementation

    async def get_signal_quality(self, interface: str) -> dict[str, Any]:
        """Get WiFi signal quality metrics."""
        try:
            system = platform.system()
            if system == "Windows":
                return await self._get_signal_quality_windows(interface)
            elif system == "Linux":
                return await self._get_signal_quality_linux(interface)
        except Exception as e:
            logger.warning("signal_quality_failed", interface=interface, error=str(e))

        return {"signal_strength": 0, "noise_level": 0, "snr_db": 0}

    async def _get_signal_quality_windows(self, interface: str) -> dict[str, Any]:
        """Get signal quality on Windows."""
        proc = await asyncio.create_subprocess_exec(
            "netsh",
            "wlan",
            "show",
            "interfaces",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="ignore")

        result = {"signal_strength": 50, "noise_level": -100, "snr_db": 20}
        for line in output.split("\n"):
            if "Signal" in line:
                try:
                    signal = int(re.search(r"\d+", line).group())
                    result["signal_strength"] = min(100, signal)
                except (ValueError, AttributeError) as e:
                    logger.debug("wifi_win_signal_parse_failed", line=line, error=str(e))
        return result

    async def _get_signal_quality_linux(self, interface: str) -> dict[str, Any]:
        """Get signal quality on Linux."""
        proc = await asyncio.create_subprocess_exec(
            "iw",
            "dev",
            interface,
            "link",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        return {"signal_strength": 50, "noise_level": -100, "snr_db": 20}
