"""
Transport Coordinator — unified orchestrator for every LAN-server
reachability channel.

Tracked transports
------------------
  1. HTTP + WebSocket (Socket.IO) — primary, managed by FastAPI/uvicorn.
     Not started here; we only probe the port to confirm it's listening.
  2. UDP Broadcast — advertised by DiscoveryService.UDPBroadcastService.
     Probed by confirming the broadcast task is alive.
  3. mDNS / Zeroconf — registered by DiscoveryService.MDNSService.
     Probed via instance liveness.
  4. Raw TCP fallback — started here. A tiny line-protocol listener on
     TCP_FALLBACK_PORT that accepts single-line commands:
         HELLO            → "OK <server_id> <version>"
         PING             → "PONG <epoch_ms>"
         DISCOVER         → JSON {name, lan_ips, port, ws_port, server_id}
         STATUS           → JSON transport health snapshot
     Line-based so a plain `nc` / `telnet` works for diagnostics.

Health model
------------
Each transport has a `TransportState`:
    name, enabled, running, port, last_ok_at, clients, error

`get_snapshot()` returns the current state for all transports — used by
the admin UI panel and the /api/transports/health endpoint.

Auto-recovery
-------------
The coordinator runs a 5s watchdog. If a transport was running and is
now down (running → False with enabled=True), it attempts restart.
The primary HTTP/WebSocket channel is not auto-restarted (uvicorn owns
it) — we only surface its state.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.lan_ice_helper import all_announce_ips, primary_lan_ip

logger = get_logger(__name__)


# ── Transport state ──────────────────────────────────────────

@dataclass
class TransportState:
    name: str
    enabled: bool = True
    running: bool = False
    port: int | None = None
    last_ok_at: float | None = None
    clients: int = 0
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "running": self.running,
            "port": self.port,
            "last_ok_at": self.last_ok_at,
            "clients": self.clients,
            "error": self.error,
            "extra": self.extra,
        }


# ── Raw TCP fallback listener ────────────────────────────────

class TcpFallbackServer:
    """
    Minimal line-oriented TCP server. Hosts `asyncio.start_server` and
    dispatches a tiny command grammar. Intended for diagnostic /
    fallback discovery use only — never for bulk data.
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._server: asyncio.base_events.Server | None = None
        self._task: asyncio.Task | None = None
        self.active_clients = 0
        self.total_clients = 0
        self.last_ok_at: float | None = None
        self._closing = False

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle, host=self.host, port=self.port,
            reuse_address=True,
        )
        logger.info(
            "tcp_fallback_started",
            host=self.host, port=self.port,
        )
        # Run serve_forever as a background task.
        self._task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        assert self._server is not None
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("tcp_fallback_serve_failed", error=str(exc))

    async def stop(self) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        logger.info("tcp_fallback_stopped")

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.active_clients += 1
        self.total_clients += 1
        peer = writer.get_extra_info("peername")
        try:
            while not reader.at_eof():
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=30.0)
                except asyncio.TimeoutError:
                    break
                if not line:
                    break
                cmd = line.decode("utf-8", errors="replace").strip().upper()
                response = await self._dispatch(cmd)
                writer.write((response + "\n").encode("utf-8"))
                await writer.drain()
                self.last_ok_at = time.time()
                if cmd in {"QUIT", "EXIT", "BYE"}:
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            logger.debug("tcp_fallback_conn_error", peer=str(peer), error=str(exc))
        finally:
            self.active_clients = max(0, self.active_clients - 1)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, cmd: str) -> str:
        s = get_settings()
        if cmd == "HELLO":
            return f"OK {s.SERVER_NAME}"
        if cmd == "PING":
            return f"PONG {int(time.time() * 1000)}"
        if cmd == "DISCOVER":
            return json.dumps({
                "name": s.SERVER_NAME,
                "primary_ip": primary_lan_ip(),
                "lan_ips": all_announce_ips(),
                "ws_port": s.PORT,
                "tcp_port": self.port,
                "udp_discovery_port": s.DISCOVERY_UDP_PORT,
            })
        if cmd == "STATUS":
            return json.dumps(transport_coordinator.get_snapshot())
        if cmd in {"QUIT", "EXIT", "BYE"}:
            return "BYE"
        return "ERR unknown_command"


# ── Coordinator ──────────────────────────────────────────────

class TransportCoordinator:
    """
    Tracks every LAN transport and provides a single health snapshot.
    """

    def __init__(self) -> None:
        self._states: dict[str, TransportState] = {
            "websocket": TransportState(name="websocket"),
            "udp_broadcast": TransportState(name="udp_broadcast"),
            "mdns": TransportState(name="mdns"),
            "tcp_fallback": TransportState(name="tcp_fallback"),
        }
        self._tcp: TcpFallbackServer | None = None
        self._watchdog: asyncio.Task | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True

        s = get_settings()

        # WebSocket state is derived from uvicorn — we just record
        # the advertised port. The watchdog will TCP-probe it.
        self._states["websocket"].port = s.PORT
        self._states["websocket"].running = True
        self._states["websocket"].last_ok_at = time.time()

        # UDP + mDNS are managed by DiscoveryService — we just reflect them.
        self._states["udp_broadcast"].port = s.DISCOVERY_UDP_PORT
        self._states["mdns"].port = 5353

        # Start the TCP fallback here.
        if s.TCP_FALLBACK_ENABLED:
            self._tcp = TcpFallbackServer("0.0.0.0", s.TCP_FALLBACK_PORT)
            try:
                await self._tcp.start()
                self._states["tcp_fallback"].running = True
                self._states["tcp_fallback"].port = s.TCP_FALLBACK_PORT
                self._states["tcp_fallback"].last_ok_at = time.time()
            except Exception as exc:
                self._states["tcp_fallback"].running = False
                self._states["tcp_fallback"].error = str(exc)
                logger.error("tcp_fallback_start_failed", error=str(exc))
        else:
            self._states["tcp_fallback"].enabled = False

        self._watchdog = asyncio.create_task(self._watchdog_loop())
        logger.info("transport_coordinator_started")

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False

        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except Exception:
                pass
            self._watchdog = None

        if self._tcp is not None:
            await self._tcp.stop()
            self._tcp = None

        logger.info("transport_coordinator_stopped")

    # ── Watchdog ─────────────────────────────────────────────

    async def _watchdog_loop(self) -> None:
        while self._started:
            try:
                await self._refresh_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("watchdog_iteration_failed", error=str(exc))
            await asyncio.sleep(5.0)

    async def _refresh_all(self) -> None:
        # WebSocket — TCP probe on PORT
        s = get_settings()
        ws_ok = await _tcp_probe("127.0.0.1", s.PORT, timeout=1.0)
        self._states["websocket"].running = ws_ok
        if ws_ok:
            self._states["websocket"].last_ok_at = time.time()
            self._states["websocket"].error = None
        else:
            self._states["websocket"].error = "port_unreachable"

        # UDP broadcast + mDNS — derive from module-level singletons.
        try:
            from app.services.discovery_service import (  # type: ignore
                udp_broadcast as _udp,
                mdns_service as _mdns,
            )
            task = getattr(_udp, "_task", None)
            udp_alive = task is not None and not task.done()
            self._states["udp_broadcast"].running = udp_alive
            if udp_alive:
                self._states["udp_broadcast"].last_ok_at = time.time()
                self._states["udp_broadcast"].error = None
            elif task is None:
                self._states["udp_broadcast"].error = "not_started"

            zc = getattr(_mdns, "_zeroconf", None)
            mdns_alive = zc is not None
            self._states["mdns"].running = mdns_alive
            if mdns_alive:
                self._states["mdns"].last_ok_at = time.time()
                self._states["mdns"].error = None
            else:
                self._states["mdns"].error = "not_registered"
        except Exception as exc:
            logger.debug("discovery_service_probe_failed", error=str(exc))

        # TCP fallback — reflect server liveness + client count.
        if self._tcp is not None:
            self._states["tcp_fallback"].running = self._tcp.running
            self._states["tcp_fallback"].clients = self._tcp.active_clients
            if self._tcp.last_ok_at is not None:
                self._states["tcp_fallback"].last_ok_at = self._tcp.last_ok_at
            self._states["tcp_fallback"].extra = {
                "total_clients": self._tcp.total_clients,
            }
            # Auto-restart if it died unexpectedly.
            if (
                self._states["tcp_fallback"].enabled
                and not self._tcp.running
                and self._started
            ):
                logger.warning("tcp_fallback_restarting")
                try:
                    await self._tcp.start()
                    self._states["tcp_fallback"].error = None
                except Exception as exc:
                    self._states["tcp_fallback"].error = str(exc)

    # ── Public API ───────────────────────────────────────────

    def get_snapshot(self) -> dict[str, Any]:
        alive = sum(1 for st in self._states.values() if st.running)
        total = sum(1 for st in self._states.values() if st.enabled)
        return {
            "summary": {
                "alive": alive,
                "total_enabled": total,
                "healthy": alive == total,
            },
            "transports": {name: st.as_dict() for name, st in self._states.items()},
            "listening_ports": sorted({st.port for st in self._states.values() if st.port}),
        }


# ── Helpers ──────────────────────────────────────────────────

async def _tcp_probe(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout,
        )
    except Exception:
        return False
    try:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    except Exception:
        pass
    return True


transport_coordinator = TransportCoordinator()

__all__ = ["TransportCoordinator", "transport_coordinator", "TransportState"]
