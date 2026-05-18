"""
Transport Detector — discovers available network transports on the system.
Performs multi-method detection across interfaces, services, and hardware.
"""

from __future__ import annotations

import asyncio
import platform
import re
import subprocess
from datetime import datetime
from typing import Optional

import psutil

from app.core.logging import get_logger
from app.transports.registry import TransportRegistry
from app.transports.types import DetectedTransport, DetectionMethod, TransportStatus

logger = get_logger(__name__)


class TransportDetector:
    """
    Singleton detector for network transports.
    Performs detection via multiple methods in parallel.
    Auto-refreshes every 30 seconds.
    """

    _instance: Optional[TransportDetector] = None
    _cached_results: list[DetectedTransport] = []
    _last_detection: datetime = None
    _lock: asyncio.Lock = None
    _refresh_task: asyncio.Task = None

    def __new__(cls, registry: Optional[TransportRegistry] = None) -> TransportDetector:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, registry: Optional[TransportRegistry] = None) -> None:
        if self._initialized:
            return

        self._initialized = True
        self._registry = registry or TransportRegistry()
        self._lock = asyncio.Lock()
        self._cached_results = []
        self._last_detection = None
        self._refresh_task = None
        logger.info("Transport detector initialized")

    async def detect_all(self) -> list[DetectedTransport]:
        """
        Run all detection methods in parallel.
        Returns combined list of detected transports.
        """
        async with self._lock:
            logger.info("Starting comprehensive transport detection")

            try:
                # Run all detection methods in parallel
                results = await asyncio.gather(
                    self._scan_network_interfaces(),
                    self._scan_wifi(),
                    self._scan_usb_devices(),
                    self._scan_serial_ports(),
                    self._probe_services(),
                    self._check_bluetooth(),
                    return_exceptions=True,
                )

                # Flatten and deduplicate
                detected = []
                seen_ids = set()

                for result in results:
                    if isinstance(result, Exception):
                        logger.warning("Detection method failed", error=str(result))
                        continue

                    if result is None:
                        continue

                    for transport in result:
                        key = (transport.transport_id, transport.interface_name)
                        if key not in seen_ids:
                            detected.append(transport)
                            seen_ids.add(key)

                self._cached_results = detected
                self._last_detection = datetime.utcnow()

                logger.info("Transport detection complete", count=len(detected))
                return detected

            except Exception as e:
                logger.error("Fatal error during detection", error=str(e))
                return self._cached_results

    async def detect_by_family(self, adapter_family: str) -> list[DetectedTransport]:
        """Detect transports of a specific adapter family."""
        all_detected = await self.detect_all()
        return [t for t in all_detected if t.adapter_family.lower() == adapter_family.lower()]

    async def _scan_network_interfaces(self) -> list[DetectedTransport]:
        """Scan system network interfaces using psutil."""
        detected = []

        try:
            # Get interface addresses
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()

            for interface_name, addr_list in addrs.items():
                if interface_name not in stats:
                    continue

                stat = stats[interface_name]
                ipv4_addr = None
                mac_addr = None
                subnet_mask = None

                # Extract IPv4 and MAC
                for addr in addr_list:
                    if addr.family.name == "AF_INET":
                        ipv4_addr = addr.address
                        subnet_mask = addr.netmask
                    elif addr.family.name == "AF_LINK":
                        mac_addr = addr.address

                # Determine transport type based on interface name
                transport_id = "ethernet"
                transport_name = "Ethernet (802.3)"
                adapter_family = "ethernet"

                if interface_name.lower().startswith(("wlan", "wifi", "wl")):
                    transport_id = "wifi_80211"
                    transport_name = "Wi-Fi (802.11)"
                    adapter_family = "wifi"
                elif interface_name.lower().startswith(("lo", "loop")):
                    continue  # Skip loopback
                elif interface_name.lower().startswith(("vlan", "tun", "tap", "veth")):
                    transport_id = "virtual"
                    transport_name = "Virtual Network"
                    adapter_family = "virtual"

                detected.append(
                    DetectedTransport(
                        transport_id=transport_id,
                        transport_name=transport_name,
                        adapter_family=adapter_family,
                        interface_name=interface_name,
                        ip_address=ipv4_addr,
                        subnet_mask=subnet_mask,
                        mac_address=mac_addr,
                        speed_mbps=float(stat.speed) if stat.speed > 0 else None,
                        is_up=stat.isup,
                        is_connected=stat.isup,
                        mtu=stat.mtu,
                        status=TransportStatus.ACTIVE if stat.isup else TransportStatus.UNAVAILABLE,
                        metadata={
                            "detection_method": DetectionMethod.INTERFACE_SCAN.value,
                            "platform": platform.system(),
                        },
                    )
                )

            logger.info("Network interfaces scanned", count=len(detected))
            return detected

        except Exception as e:
            logger.warning("Network interface scan failed", error=str(e))
            return []

    async def _scan_wifi(self) -> list[DetectedTransport]:
        """Scan Wi-Fi networks."""
        detected = []

        try:
            if platform.system() == "Windows":
                return await self._scan_wifi_windows()
            else:
                return await self._scan_wifi_linux()
        except Exception as e:
            logger.warning("Wi-Fi scan failed", error=str(e))
            return []

    async def _scan_wifi_windows(self) -> list[DetectedTransport]:
        """Scan Wi-Fi on Windows using netsh."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "netsh", "wlan", "show", "networks",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode("utf-8", errors="ignore")

            # Parse SSID from output
            ssids = re.findall(r"SSID\s+:\s+(.+?)$", output, re.MULTILINE)

            if ssids:
                return [
                    DetectedTransport(
                        transport_id="wifi_80211",
                        transport_name="Wi-Fi (802.11)",
                        adapter_family="wifi",
                        interface_name=f"wifi_{i}",
                        is_up=True,
                        is_connected=True,
                        status=TransportStatus.ACTIVE,
                        metadata={
                            "detection_method": DetectionMethod.DRIVER_CHECK.value,
                            "ssid": ssid.strip(),
                        },
                    )
                    for i, ssid in enumerate(ssids)
                ]
        except Exception as e:
            logger.warning("Windows Wi-Fi scan failed", error=str(e))

        return []

    async def _scan_wifi_linux(self) -> list[DetectedTransport]:
        """Scan Wi-Fi on Linux using iwconfig or iw."""
        detected = []

        try:
            # Try iw first (preferred)
            proc = await asyncio.create_subprocess_exec(
                "iw", "dev",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode("utf-8", errors="ignore")

            interfaces = re.findall(r"Interface\s+(\w+)", output)

            for interface in interfaces:
                detected.append(
                    DetectedTransport(
                        transport_id="wifi_80211",
                        transport_name="Wi-Fi (802.11)",
                        adapter_family="wifi",
                        interface_name=interface,
                        is_up=True,
                        is_connected=True,
                        status=TransportStatus.ACTIVE,
                        metadata={
                            "detection_method": DetectionMethod.DRIVER_CHECK.value,
                        },
                    )
                )

        except Exception as e:
            logger.warning("Linux Wi-Fi scan failed", error=str(e))

        return detected

    async def _scan_usb_devices(self) -> list[DetectedTransport]:
        """
        Scan for USB network adapters.
        Windows: invoke Get-PnpDevice via powershell.exe (it's a cmdlet, not an exe).
        Linux: lsusb.
        """
        detected = []

        try:
            if platform.system() == "Windows":
                # Get-PnpDevice is a PowerShell cmdlet — must be invoked via powershell
                proc = await asyncio.create_subprocess_exec(
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "Get-PnpDevice -Class Net -Status OK | Select-Object -ExpandProperty FriendlyName",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    "lsusb",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=4.0)
            except asyncio.TimeoutError:
                logger.debug("usb scan timeout — killing subprocess")
                try:
                    proc.kill()
                except Exception:
                    pass
                return []

            output = stdout.decode("utf-8", errors="ignore")

            # Only emit when we see actual USB network device names
            usb_keywords = ("USB", "usb", "Wireless USB", "Ethernet USB")
            if any(k in output for k in usb_keywords):
                detected.append(
                    DetectedTransport(
                        transport_id="usb",
                        transport_name="USB Network Adapter",
                        adapter_family="usb",
                        interface_name="usb_net",
                        is_up=True,
                        is_connected=False,
                        status=TransportStatus.AVAILABLE,
                        metadata={
                            "detection_method": DetectionMethod.HARDWARE_PROBE.value,
                            "evidence_lines": output.count("\n"),
                        },
                    )
                )

        except FileNotFoundError as e:
            logger.debug("USB scan tool missing", error=str(e))
        except Exception as e:
            logger.debug("USB scan failed", error=str(e))

        return detected

    async def _scan_serial_ports(self) -> list[DetectedTransport]:
        """Scan for serial ports."""
        detected = []

        try:
            if platform.system() == "Windows":
                # Windows COM ports
                import winreg
                reg = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
                key = winreg.OpenKey(reg, r"HARDWARE\DEVICEMAP\SERIALCOMM")
                for i in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _ = winreg.EnumValue(key, i)
                    detected.append(
                        DetectedTransport(
                            transport_id="serial",
                            transport_name="Serial (RS-232)",
                            adapter_family="serial",
                            interface_name=value,
                            is_up=True,
                            is_connected=False,
                            status=TransportStatus.AVAILABLE,
                            metadata={
                                "detection_method": DetectionMethod.INTERFACE_SCAN.value,
                            },
                        )
                    )
            else:
                # Linux ttyS/ttyUSB ports
                import glob
                for port in glob.glob("/dev/tty[SU]*"):
                    detected.append(
                        DetectedTransport(
                            transport_id="serial",
                            transport_name="Serial (RS-232)",
                            adapter_family="serial",
                            interface_name=port,
                            is_up=True,
                            is_connected=False,
                            status=TransportStatus.AVAILABLE,
                            metadata={
                                "detection_method": DetectionMethod.INTERFACE_SCAN.value,
                            },
                        )
                    )

        except Exception as e:
            logger.debug("Serial port scan failed", error=str(e))

        return detected

    async def _probe_services(self) -> list[DetectedTransport]:
        """
        Probe for known service ports on localhost.
        Only reports services that ACTUALLY accept a TCP connection — no
        more silent fake "always available" entries.
        """
        detected = []
        ports_to_check = {
            502: ("modbus", "Modbus (TCP)", "modbus"),
            47808: ("bacnet", "BACnet", "bacnet"),
            9600: ("custom", "Custom Service", "custom"),
        }

        async def _try_connect(host: str, port: int, timeout: float = 0.3) -> bool:
            """Return True if a TCP connection to host:port completes within timeout."""
            try:
                fut = asyncio.open_connection(host, port)
                reader, writer = await asyncio.wait_for(fut, timeout=timeout)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return True
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
                return False
            except Exception as e:
                logger.debug("port probe error", port=port, error=str(e))
                return False

        for port, (transport_id, name, family) in ports_to_check.items():
            try:
                is_listening = await _try_connect("127.0.0.1", port)
                if not is_listening:
                    continue
                detected.append(
                    DetectedTransport(
                        transport_id=transport_id,
                        transport_name=name,
                        adapter_family=family,
                        interface_name=f"service_port_{port}",
                        is_up=True,
                        is_connected=True,
                        status=TransportStatus.ACTIVE,
                        metadata={
                            "detection_method": DetectionMethod.PORT_SCAN.value,
                            "port": port,
                            "host": "127.0.0.1",
                        },
                    )
                )
            except Exception as e:
                logger.debug("Service probe iteration failed", port=port, error=str(e))

        return detected

    async def _check_bluetooth(self) -> list[DetectedTransport]:
        """Check for Bluetooth and BLE."""
        detected = []

        try:
            if platform.system() == "Linux":
                # Use hciconfig to check Bluetooth
                proc = await asyncio.create_subprocess_exec(
                    "hciconfig",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                output = stdout.decode("utf-8", errors="ignore")

                devices = re.findall(r"(hci\d+):", output)
                for device in devices:
                    detected.append(
                        DetectedTransport(
                            transport_id="bluetooth_le",
                            transport_name="Bluetooth Low Energy",
                            adapter_family="bluetooth",
                            interface_name=device,
                            is_up=True,
                            is_connected=False,
                            status=TransportStatus.AVAILABLE,
                            metadata={
                                "detection_method": DetectionMethod.HARDWARE_PROBE.value,
                            },
                        )
                    )

        except Exception as e:
            logger.debug("Bluetooth check failed", error=str(e))

        return detected

    async def measure_signal_quality(self, transport: DetectedTransport) -> Optional[dict]:
        """
        Measure signal quality (latency, jitter, packet loss).
        Real measurement: 4 UDP-connect samples to derive RTT statistics.
        Returns None only if the transport has no IP to probe.
        """
        try:
            if not transport.ip_address:
                return None

            samples: list[float] = []
            loop = asyncio.get_event_loop()

            for _ in range(4):
                start = __import__("time").perf_counter()
                sock = None
                try:
                    sock = __import__("socket").socket(
                        __import__("socket").AF_INET,
                        __import__("socket").SOCK_DGRAM,
                    )
                    sock.setblocking(False)
                    await loop.run_in_executor(
                        None,
                        lambda s=sock: s.connect((transport.ip_address, 1)),
                    )
                    samples.append(
                        (__import__("time").perf_counter() - start) * 1000.0
                    )
                except Exception as inner:
                    logger.debug(
                        "signal_sample_failed",
                        interface=transport.interface_name,
                        error=str(inner),
                    )
                finally:
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
                await asyncio.sleep(0.01)

            if not samples:
                return {
                    "transport_id": transport.transport_id,
                    "latency_ms": 0.0,
                    "jitter_ms": 0.0,
                    "packet_loss_percent": 100.0,
                    "samples": 0,
                }

            avg = sum(samples) / len(samples)
            jitter = max(samples) - min(samples) if len(samples) > 1 else 0.0
            return {
                "transport_id": transport.transport_id,
                "latency_ms": round(avg, 3),
                "jitter_ms": round(jitter, 3),
                "packet_loss_percent": round((1 - len(samples) / 4) * 100, 1),
                "samples": len(samples),
            }

        except Exception as e:
            logger.warning("Signal quality measurement failed", error=str(e))
            return None

    def get_cached_results(self) -> list[DetectedTransport]:
        """Get last detection results."""
        return self._cached_results.copy()

    def get_best_transport(self) -> Optional[DetectedTransport]:
        """
        Get highest quality available transport.
        Prioritizes: active > available, connected > disconnected.
        """
        if not self._cached_results:
            return None

        # Score transports by status and connectivity
        def score_transport(t: DetectedTransport) -> tuple:
            status_score = {
                TransportStatus.ACTIVE: 4,
                TransportStatus.DEGRADED: 2,
                TransportStatus.AVAILABLE: 1,
                TransportStatus.UNAVAILABLE: 0,
                TransportStatus.ERROR: -1,
            }.get(t.status, 0)

            connected_score = 1 if t.is_connected else 0
            speed_score = (t.speed_mbps or 0) / 1000  # Normalize

            return (status_score, connected_score, speed_score)

        return max(self._cached_results, key=score_transport, default=None)

    async def start_auto_refresh(self, interval_seconds: int = 30) -> None:
        """Start background auto-refresh task."""
        if self._refresh_task and not self._refresh_task.done():
            return

        async def refresh_loop():
            while True:
                try:
                    await asyncio.sleep(interval_seconds)
                    await self.detect_all()
                except asyncio.CancelledError:
                    logger.info("Auto-refresh stopped")
                    break
                except Exception as e:
                    logger.error("Auto-refresh error", error=str(e))

        self._refresh_task = asyncio.create_task(refresh_loop())
        logger.info("Auto-refresh started", interval_seconds=interval_seconds)

    async def stop_auto_refresh(self) -> None:
        """Stop background auto-refresh task."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
            logger.info("Auto-refresh stopped")
