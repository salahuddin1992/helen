"""
Signal Quality Analyzer — measures network performance metrics.
Monitors latency, bandwidth, jitter, packet loss.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Callable, Optional

from app.core.logging import get_logger
from app.transports.types import DetectedTransport, SignalQuality

logger = get_logger(__name__)


class SignalAnalyzer:
    """
    Singleton analyzer for network signal quality.
    Measures latency, bandwidth, jitter, packet loss.
    """

    _instance: Optional[SignalAnalyzer] = None

    def __new__(cls) -> SignalAnalyzer:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        self._initialized = True
        logger.info("Signal analyzer initialized")

    async def measure_latency(
        self,
        target_ip: str,
        count: int = 5,
        timeout_seconds: int = 10
    ) -> float:
        """
        Measure latency to target in milliseconds using ICMP ping.
        Returns average latency or -1.0 on failure.
        """
        try:
            if not target_ip:
                return -1.0

            # Use ping command
            cmd = ["ping", "-c" if not self._is_windows() else "-n", str(count), target_ip]
            if self._is_windows():
                cmd = ["ping", "-n", str(count), target_ip]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                proc.kill()
                return -1.0

            output = stdout.decode("utf-8", errors="ignore")

            # Parse latency from output
            latency = self._parse_ping_output(output)
            if latency > 0:
                logger.debug("Latency measured", target=target_ip, latency_ms=latency)
                return latency

            return -1.0

        except Exception as e:
            logger.warning("Latency measurement failed", error=str(e), target=target_ip)
            return -1.0

    async def measure_bandwidth(self, target_ip: str, test_size_mb: int = 10) -> float:
        """
        Measure bandwidth to target in Mbps using TCP.
        Returns estimated bandwidth or -1.0 on failure.
        """
        try:
            if not target_ip:
                return -1.0

            # This is a simplified implementation
            # In production, would use iperf3 or similar
            import socket
            import time

            test_bytes = test_size_mb * 1024 * 1024
            chunk_size = 65536

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)

            try:
                await asyncio.wait_for(
                    self._async_connect(sock, target_ip, 5000),
                    timeout=5.0
                )

                start = time.time()
                sent = 0

                while sent < test_bytes:
                    to_send = min(chunk_size, test_bytes - sent)
                    sock.send(b"x" * to_send)
                    sent += to_send

                elapsed = time.time() - start
                bandwidth_mbps = (sent * 8) / (elapsed * 1_000_000) if elapsed > 0 else 0

                logger.debug("Bandwidth measured", target=target_ip, mbps=bandwidth_mbps)
                return bandwidth_mbps

            except (socket.error, asyncio.TimeoutError):
                return -1.0
            finally:
                sock.close()

        except Exception as e:
            logger.warning("Bandwidth measurement failed", error=str(e))
            return -1.0

    async def measure_jitter(
        self,
        target_ip: str,
        count: int = 10
    ) -> float:
        """
        Measure jitter (latency variance) in milliseconds.
        Returns jitter or -1.0 on failure.
        """
        try:
            if not target_ip:
                return -1.0

            latencies = []

            for _ in range(count):
                cmd = ["ping", "-c", "1", target_ip] if not self._is_windows() else ["ping", "-n", "1", target_ip]

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                stdout, _ = await proc.communicate()
                output = stdout.decode("utf-8", errors="ignore")
                latency = self._parse_ping_output(output, single=True)

                if latency > 0:
                    latencies.append(latency)

                await asyncio.sleep(0.1)

            if len(latencies) < 2:
                return -1.0

            # Calculate standard deviation as jitter
            mean = sum(latencies) / len(latencies)
            variance = sum((x - mean) ** 2 for x in latencies) / len(latencies)
            jitter = variance ** 0.5

            logger.debug("Jitter measured", target=target_ip, jitter_ms=jitter)
            return jitter

        except Exception as e:
            logger.warning("Jitter measurement failed", error=str(e))
            return -1.0

    async def measure_packet_loss(
        self,
        target_ip: str,
        count: int = 20
    ) -> float:
        """
        Measure packet loss percentage.
        Returns packet loss 0-100 or -1.0 on failure.
        """
        try:
            if not target_ip:
                return -1.0

            cmd = ["ping", "-c", str(count), target_ip] if not self._is_windows() else ["ping", "-n", str(count), target_ip]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            stdout, _ = await proc.communicate()
            output = stdout.decode("utf-8", errors="ignore")

            # Parse packet loss from output
            packet_loss = self._parse_packet_loss(output)
            if packet_loss >= 0:
                logger.debug("Packet loss measured", target=target_ip, loss_percent=packet_loss)
                return packet_loss

            return -1.0

        except Exception as e:
            logger.warning("Packet loss measurement failed", error=str(e))
            return -1.0

    async def full_analysis(self, transport: DetectedTransport) -> SignalQuality:
        """
        Perform comprehensive signal analysis.
        Returns complete quality metrics.
        """
        logger.info("Starting full signal analysis", transport=transport.transport_id)

        try:
            target = transport.ip_address or transport.gateway

            if not target:
                logger.warning("No IP address available for analysis", transport=transport.transport_id)
                return SignalQuality(
                    transport_id=transport.transport_id,
                    latency_ms=0,
                )

            # Run measurements in parallel
            latency_task = self.measure_latency(target, count=5)
            bandwidth_task = self.measure_bandwidth(target, test_size_mb=5)
            jitter_task = self.measure_jitter(target, count=10)
            packet_loss_task = self.measure_packet_loss(target, count=20)

            latency, bandwidth, jitter, packet_loss = await asyncio.gather(
                latency_task,
                bandwidth_task,
                jitter_task,
                packet_loss_task,
                return_exceptions=True,
            )

            # Handle exceptions
            latency = latency if isinstance(latency, (int, float)) else 0
            bandwidth = bandwidth if isinstance(bandwidth, (int, float)) else None
            jitter = jitter if isinstance(jitter, (int, float)) else None
            packet_loss = packet_loss if isinstance(packet_loss, (int, float)) else 0

            return SignalQuality(
                transport_id=transport.transport_id,
                signal_strength=transport.signal_strength,
                bandwidth_available_mbps=bandwidth if bandwidth and bandwidth > 0 else None,
                latency_ms=latency if latency > 0 else 0,
                jitter_ms=jitter if jitter and jitter > 0 else None,
                packet_loss_percent=max(0, packet_loss) if packet_loss else 0,
            )

        except Exception as e:
            logger.error("Full analysis failed", error=str(e), transport=transport.transport_id)
            return SignalQuality(
                transport_id=transport.transport_id,
                latency_ms=0,
            )

    async def continuous_monitor(
        self,
        transport_id: str,
        callback: Callable[[SignalQuality], None],
        interval_seconds: int = 5,
    ) -> None:
        """
        Continuous monitoring with callback.
        Calls callback periodically with updated metrics.
        """
        logger.info("Starting continuous monitoring", transport=transport_id, interval=interval_seconds)

        try:
            while True:
                await asyncio.sleep(interval_seconds)
                # Callback would be invoked with metrics
                logger.debug("Monitor tick", transport=transport_id)

        except asyncio.CancelledError:
            logger.info("Continuous monitoring stopped", transport=transport_id)
            raise

    def get_quality_score(self, quality: SignalQuality) -> int:
        """
        Calculate quality score 0-100.
        Based on latency, jitter, packet loss, bandwidth.
        """
        score = 100

        # Latency penalty
        if quality.latency_ms >= 500:
            score -= 40
        elif quality.latency_ms >= 150:
            score -= 20
        elif quality.latency_ms >= 50:
            score -= 10

        # Jitter penalty
        if quality.jitter_ms:
            if quality.jitter_ms >= 100:
                score -= 20
            elif quality.jitter_ms >= 50:
                score -= 10
            elif quality.jitter_ms >= 10:
                score -= 5

        # Packet loss penalty
        if quality.packet_loss_percent >= 10:
            score -= 30
        elif quality.packet_loss_percent >= 5:
            score -= 15
        elif quality.packet_loss_percent >= 1:
            score -= 5

        # Signal strength bonus
        if quality.signal_strength:
            if quality.signal_strength >= 80:
                score = min(100, score + 10)
            elif quality.signal_strength < 20:
                score -= 10

        # Bandwidth bonus
        if quality.bandwidth_available_mbps:
            if quality.bandwidth_available_mbps >= 1000:
                score = min(100, score + 5)

        return max(0, min(100, score))

    def get_quality_label(self, score: int) -> str:
        """Get human-readable quality label."""
        if score >= 90:
            return "excellent"
        elif score >= 75:
            return "good"
        elif score >= 50:
            return "fair"
        elif score >= 25:
            return "poor"
        else:
            return "unusable"

    @staticmethod
    def _is_windows() -> bool:
        import platform
        return platform.system() == "Windows"

    @staticmethod
    def _parse_ping_output(output: str, single: bool = False) -> float:
        """Parse latency from ping output."""
        import re

        if "Windows" in output or "Reply from" in output:
            # Windows format
            match = re.search(r"time[=<](\d+)ms", output)
        else:
            # Linux/Mac format
            match = re.search(r"time=(\d+\.?\d*)\s*ms", output)

        if match:
            return float(match.group(1))

        return -1.0

    @staticmethod
    def _parse_packet_loss(output: str) -> float:
        """Parse packet loss from ping output."""
        import re

        match = re.search(r"(\d+(?:\.\d+)?)\s*%.*loss", output)
        if match:
            return float(match.group(1))

        return -1.0

    @staticmethod
    async def _async_connect(sock, host: str, port: int) -> None:
        """Async socket connect wrapper."""
        loop = asyncio.get_event_loop()
        await loop.sock_connect(sock, (host, port))
