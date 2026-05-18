"""
MetricsCollector — production-grade system and transport health collection.

Responsibilities
----------------
- Sample host metrics (CPU, memory, network IO, disk IO) via ``psutil``.
- Maintain a 5-minute rolling window (300 samples at 1Hz) in memory.
- Probe per-transport health (NATS, MQTT, ZeroMQ, RabbitMQ, gRPC, WireGuard, SSH)
  with a short TTL cache to avoid request-storms.
- Track service-side counters (rps, error rate, alerts) fed by middleware.

Design notes
------------
- All public APIs are async. CPU sampling is offloaded to a thread to avoid
  blocking the event loop on psutil's first probe.
- Probes are intentionally fail-soft: a probe failure produces a ``degraded``
  or ``down`` status but never raises out of ``transport_status``.
- The collector is a singleton accessed via ``get_metrics_collector()``;
  multiple imports / lifespans converge on the same instance.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Optional

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover — psutil is a hard dependency
    psutil = None  # type: ignore[assignment]

try:
    import structlog
    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    _log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────
WINDOW_SECONDS: int = 300            # 5-minute window
TICK_SECONDS: float = 1.0            # 1Hz sampling
PROBE_TTL_SECONDS: float = 2.0       # transport probe cache lifetime
MAX_ALERTS: int = 50

SUPPORTED_TRANSPORTS: tuple[str, ...] = (
    "nats", "mqtt", "zeromq", "rabbitmq", "grpc", "wireguard", "ssh",
)


# ── Data classes ─────────────────────────────────────────────────────────


@dataclass
class MetricSample:
    """A single 1-second snapshot of host + service metrics."""
    ts: float
    cpu: float                   # 0-100
    mem: float                   # 0-100
    net_in_mbps: float
    net_out_mbps: float
    disk_io_mbps: float
    rps: float
    errors: float                # errors/sec over last 60s
    rtt_ms: float                # service-side RTT EWMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "cpu": round(self.cpu, 2),
            "mem": round(self.mem, 2),
            "net_in_mbps": round(self.net_in_mbps, 3),
            "net_out_mbps": round(self.net_out_mbps, 3),
            "disk_io_mbps": round(self.disk_io_mbps, 3),
            "rps": round(self.rps, 2),
            "errors": round(self.errors, 2),
            "rtt_ms": round(self.rtt_ms, 2),
        }


@dataclass
class Alert:
    severity: str               # info|warning|critical
    message: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "message": self.message,
            "timestamp": self.timestamp,
        }


@dataclass
class TransportStatus:
    name: str
    status: str                  # healthy|degraded|down
    msg_per_sec: float
    conn_count: int
    latency_p50_ms: float
    latency_p99_ms: float
    tags: list[str] = field(default_factory=list)
    last_checked: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "msg_per_sec": round(self.msg_per_sec, 2),
            "conn_count": self.conn_count,
            "latency_p50_ms": round(self.latency_p50_ms, 2),
            "latency_p99_ms": round(self.latency_p99_ms, 2),
            "tags": self.tags,
            "last_checked": self.last_checked,
        }


# ── Probe registry (pluggable) ───────────────────────────────────────────

ProbeFn = Callable[[], Awaitable[TransportStatus]]


# ── MetricsCollector ─────────────────────────────────────────────────────


class MetricsCollector:
    """Singleton collector. Use :func:`get_metrics_collector`."""

    def __init__(self) -> None:
        self._window: Deque[MetricSample] = deque(maxlen=WINDOW_SECONDS)
        self._alerts: Deque[Alert] = deque(maxlen=MAX_ALERTS)
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None
        self._running: bool = False

        # Service-side rolling counters
        self._req_count_60s: Deque[float] = deque(maxlen=60)  # ts of each request
        self._err_count_60s: Deque[float] = deque(maxlen=600)
        self._rtt_ewma_ms: float = 0.0
        self._rtt_alpha: float = 0.2

        # psutil baseline for delta IO
        self._last_net_io: Optional[Any] = None
        self._last_disk_io: Optional[Any] = None
        self._last_io_ts: float = 0.0

        # Transport probe cache
        self._probe_cache: dict[str, tuple[float, TransportStatus]] = {}
        self._probes: dict[str, ProbeFn] = {}
        self._register_default_probes()

    # ── Public lifecycle ────────────────────────────────────────────────

    async def start_collector(self) -> None:
        """Start the 1Hz sampling background task. Idempotent."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._collector_loop(), name="metrics_collector")
        _log.info("metrics_collector_started")

    async def stop_collector(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        _log.info("metrics_collector_stopped")

    # ── Counter hooks (called by HTTP middleware) ───────────────────────

    def record_request(self, rtt_ms: float = 0.0, error: bool = False) -> None:
        now = time.time()
        self._req_count_60s.append(now)
        if error:
            self._err_count_60s.append(now)
        if rtt_ms > 0:
            if self._rtt_ewma_ms == 0:
                self._rtt_ewma_ms = rtt_ms
            else:
                self._rtt_ewma_ms = (
                    self._rtt_alpha * rtt_ms
                    + (1 - self._rtt_alpha) * self._rtt_ewma_ms
                )

    def push_alert(self, severity: str, message: str) -> None:
        self._alerts.append(Alert(
            severity=severity, message=message, timestamp=time.time(),
        ))

    # ── Snapshot API ────────────────────────────────────────────────────

    async def collect_current(self) -> dict[str, Any]:
        """Return the most recent snapshot + alerts in dashboard format."""
        async with self._lock:
            if self._window:
                latest = self._window[-1]
            else:
                latest = await self._sample_once()
            alerts = [a.to_dict() for a in list(self._alerts)[-MAX_ALERTS:]]

        return {
            **latest.to_dict(),
            "alerts": alerts,
            "window_size": len(self._window),
        }

    async def window_snapshot(self, last_n: int = 300) -> list[dict[str, Any]]:
        """Return a list of recent samples (oldest -> newest)."""
        async with self._lock:
            snap = list(self._window)[-last_n:]
        return [s.to_dict() for s in snap]

    # ── Transport probes ────────────────────────────────────────────────

    def register_probe(self, name: str, fn: ProbeFn) -> None:
        self._probes[name.lower()] = fn

    async def transport_status(self, name: str) -> TransportStatus:
        """
        Cached probe. TTL=2s. Returns an object even if the probe fails
        (status='down', tags=['probe_error']).
        """
        key = name.lower()
        now = time.time()
        cached = self._probe_cache.get(key)
        if cached and (now - cached[0]) < PROBE_TTL_SECONDS:
            return cached[1]

        probe = self._probes.get(key)
        if probe is None:
            status_obj = TransportStatus(
                name=key, status="down",
                msg_per_sec=0.0, conn_count=0,
                latency_p50_ms=0.0, latency_p99_ms=0.0,
                tags=["unknown_transport"], last_checked=now,
            )
        else:
            try:
                status_obj = await asyncio.wait_for(probe(), timeout=2.0)
                status_obj.last_checked = now
            except asyncio.TimeoutError:
                status_obj = TransportStatus(
                    name=key, status="degraded",
                    msg_per_sec=0.0, conn_count=0,
                    latency_p50_ms=0.0, latency_p99_ms=0.0,
                    tags=["probe_timeout"], last_checked=now,
                )
            except Exception as exc:
                _log.warning("transport_probe_failed", transport=key, error=str(exc))
                status_obj = TransportStatus(
                    name=key, status="down",
                    msg_per_sec=0.0, conn_count=0,
                    latency_p50_ms=0.0, latency_p99_ms=0.0,
                    tags=["probe_error", str(exc)[:80]],
                    last_checked=now,
                )

        self._probe_cache[key] = (now, status_obj)
        return status_obj

    # ── Internal: sampling loop ─────────────────────────────────────────

    async def _collector_loop(self) -> None:
        try:
            while self._running:
                start = time.monotonic()
                try:
                    sample = await self._sample_once()
                    async with self._lock:
                        self._window.append(sample)
                    self._evaluate_alerts(sample)
                except Exception as exc:
                    _log.error("metrics_sample_error", error=str(exc))
                # Sleep the remainder of the tick
                elapsed = time.monotonic() - start
                await asyncio.sleep(max(0.0, TICK_SECONDS - elapsed))
        except asyncio.CancelledError:
            raise

    async def _sample_once(self) -> MetricSample:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sample_blocking)

    def _sample_blocking(self) -> MetricSample:
        now = time.time()
        cpu = 0.0
        mem = 0.0
        net_in = 0.0
        net_out = 0.0
        disk_io = 0.0

        if psutil is not None:
            try:
                cpu = float(psutil.cpu_percent(interval=None))
                mem = float(psutil.virtual_memory().percent)
            except Exception:
                pass
            try:
                net = psutil.net_io_counters()
                disk = psutil.disk_io_counters()
                dt = max(now - self._last_io_ts, 1e-6) if self._last_io_ts else 1.0
                if self._last_net_io is not None and net is not None:
                    rx = max(net.bytes_recv - self._last_net_io.bytes_recv, 0)
                    tx = max(net.bytes_sent - self._last_net_io.bytes_sent, 0)
                    net_in = (rx * 8.0) / (dt * 1_000_000)
                    net_out = (tx * 8.0) / (dt * 1_000_000)
                if self._last_disk_io is not None and disk is not None:
                    rb = max(disk.read_bytes - self._last_disk_io.read_bytes, 0)
                    wb = max(disk.write_bytes - self._last_disk_io.write_bytes, 0)
                    disk_io = ((rb + wb) * 8.0) / (dt * 1_000_000)
                self._last_net_io = net
                self._last_disk_io = disk
                self._last_io_ts = now
            except Exception:
                pass

        # Rolling RPS (requests in last 60s) and errors/sec
        cutoff = now - 60.0
        while self._req_count_60s and self._req_count_60s[0] < cutoff:
            self._req_count_60s.popleft()
        while self._err_count_60s and self._err_count_60s[0] < cutoff:
            self._err_count_60s.popleft()
        rps = len(self._req_count_60s) / 60.0
        errors = len(self._err_count_60s) / 60.0

        return MetricSample(
            ts=now,
            cpu=cpu, mem=mem,
            net_in_mbps=net_in, net_out_mbps=net_out,
            disk_io_mbps=disk_io,
            rps=rps, errors=errors,
            rtt_ms=self._rtt_ewma_ms,
        )

    # ── Alert evaluation ─────────────────────────────────────────────────

    def _evaluate_alerts(self, sample: MetricSample) -> None:
        if sample.cpu > 90:
            self.push_alert("critical", f"CPU at {sample.cpu:.1f}%")
        elif sample.cpu > 75:
            self.push_alert("warning", f"CPU elevated: {sample.cpu:.1f}%")
        if sample.mem > 90:
            self.push_alert("critical", f"Memory at {sample.mem:.1f}%")
        if sample.errors > 5:
            self.push_alert("warning", f"Error rate {sample.errors:.1f}/s")
        if sample.rtt_ms > 1000:
            self.push_alert("warning", f"High RTT {sample.rtt_ms:.0f}ms")

    # ── Default transport probes ─────────────────────────────────────────

    def _register_default_probes(self) -> None:
        self._probes["nats"] = self._probe_tcp_factory("nats", 4222, ["pub/sub", "queue"])
        self._probes["mqtt"] = self._probe_tcp_factory("mqtt", 1883, ["iot", "pub/sub"])
        self._probes["zeromq"] = self._probe_tcp_factory("zeromq", 5555, ["pipeline"])
        self._probes["rabbitmq"] = self._probe_tcp_factory("rabbitmq", 5672, ["amqp", "queue"])
        self._probes["grpc"] = self._probe_tcp_factory("grpc", 50051, ["rpc"])
        self._probes["wireguard"] = self._probe_udp_factory("wireguard", 51820, ["vpn", "udp"])
        self._probes["ssh"] = self._probe_tcp_factory("ssh", 22, ["mgmt"])

    def _probe_tcp_factory(self, name: str, default_port: int, tags: list[str]) -> ProbeFn:
        env_key = f"{name.upper()}_PROBE"
        async def probe() -> TransportStatus:
            host, port = _resolve_endpoint(env_key, default_port)
            t0 = time.monotonic()
            status = "down"
            try:
                fut = asyncio.open_connection(host=host, port=port)
                reader, writer = await asyncio.wait_for(fut, timeout=1.0)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                status = "healthy"
            except Exception:
                status = "down"
            lat_ms = (time.monotonic() - t0) * 1000.0
            return TransportStatus(
                name=name, status=status,
                msg_per_sec=0.0, conn_count=(1 if status == "healthy" else 0),
                latency_p50_ms=lat_ms, latency_p99_ms=lat_ms * 1.5,
                tags=list(tags) + [f"endpoint={host}:{port}"],
            )
        return probe

    def _probe_udp_factory(self, name: str, default_port: int, tags: list[str]) -> ProbeFn:
        env_key = f"{name.upper()}_PROBE"
        async def probe() -> TransportStatus:
            host, port = _resolve_endpoint(env_key, default_port)
            # UDP is connectionless; we only check that the socket binds & a
            # send doesn't raise — this is a liveness signal, not a handshake.
            t0 = time.monotonic()
            status = "degraded"
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(0.5)
                try:
                    sock.sendto(b"\x00", (host, port))
                    status = "healthy"
                finally:
                    sock.close()
            except Exception:
                status = "down"
            lat_ms = (time.monotonic() - t0) * 1000.0
            return TransportStatus(
                name=name, status=status,
                msg_per_sec=0.0, conn_count=0,
                latency_p50_ms=lat_ms, latency_p99_ms=lat_ms * 1.5,
                tags=list(tags) + [f"endpoint={host}:{port}"],
            )
        return probe


def _resolve_endpoint(env_key: str, default_port: int) -> tuple[str, int]:
    """Resolve a probe endpoint from env (host:port) or default to localhost."""
    raw = os.getenv(env_key, "").strip()
    if raw:
        if ":" in raw:
            host, _, port_s = raw.rpartition(":")
            try:
                return host or "127.0.0.1", int(port_s)
            except ValueError:
                return raw, default_port
        return raw, default_port
    return "127.0.0.1", default_port


# ── Singleton accessor ──────────────────────────────────────────────────

_INSTANCE: Optional[MetricsCollector] = None


def get_metrics_collector() -> MetricsCollector:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = MetricsCollector()
    return _INSTANCE
