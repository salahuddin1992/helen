"""
LAN peer registry — tracks other CommClient-Server instances discovered via
UDP broadcast on the local network.

Each peer is any process broadcasting a `type: "commclient-server"` JSON
packet on `DISCOVERY_UDP_PORT`. We filter out our own broadcasts (by
`server_id`) and keep a small in-memory table keyed by `server_id` with a
short TTL so the list reflects the *current* LAN state.

Wired into startup via `udp_listener_service.start()` / `.stop()` in main.py.
Exposed to clients through /api/peers.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.discovery_service import get_server_id

logger = get_logger(__name__)
settings = get_settings()


# A peer is considered stale if we haven't heard from it for this many seconds.
# 5× the default broadcast interval gives us enough margin to survive a dropped
# packet or two without flapping.
PEER_TTL_SECONDS = max(15, settings.DISCOVERY_BROADCAST_INTERVAL * 5)


@dataclass
class PeerRecord:
    server_id: str
    name: str
    host: str
    port: int
    version: str
    protocol: str
    users_online: int
    uptime: int
    first_seen: float
    last_seen: float
    from_ip: str  # address we actually received the broadcast from
    # Bridge metadata — populated from broadcast payload's `bridge` and
    # `host_aliases` fields. A bridge=True peer can relay traffic
    # between subnets it spans, which the cross-router relay router
    # uses to find a path when direct connection fails.
    bridge: bool = False
    host_aliases: list = field(default_factory=list)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.last_seen

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > PEER_TTL_SECONDS

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["age_seconds"] = round(self.age_seconds, 2)
        d["is_stale"] = self.is_stale
        return d


class PeerRegistry:
    """Thread-safe (via asyncio lock) in-memory peer table.

    Peer cache persistence
    ----------------------
    Every peer we successfully discover is also written to disk at
    ``data/peers_cache.json``. On the next boot we replay the cache so
    the cluster_mesh.persistent-retry loop has a peer list to probe
    immediately, even before any UDP broadcast arrives. This makes
    the server "remember" everyone it ever talked to, so a relaunch
    after a network outage reconnects to the whole previous mesh
    within seconds — without needing a single seed peer in the env.
    """

    def __init__(self) -> None:
        self._peers: dict[str, PeerRecord] = {}
        self._lock = asyncio.Lock()
        self._cache_path = self._resolve_cache_path()
        self._dirty = False
        self._last_persist_ts = 0.0
        # Background tasks we spawn (auto-peer enrollment, etc.). We
        # keep strong references in this set so the asyncio garbage
        # collector doesn't kill them mid-flight; tasks remove
        # themselves on completion.
        self._bg_tasks: set[asyncio.Task] = set()
        # Hydrate from disk synchronously — cheap (small JSON) and gives
        # cluster_mesh a head start on reconnects.
        self._load_cache()

    @staticmethod
    def _resolve_cache_path():
        """Return the on-disk cache path under the live data dir."""
        from pathlib import Path
        import os as _os_pc
        data_dir = _os_pc.environ.get("COMMCLIENT_DATA_DIR")
        if data_dir:
            base = Path(data_dir)
        else:
            base = Path(__file__).resolve().parents[2] / "data"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return base / "peers_cache.json"

    def _load_cache(self) -> None:
        """Best-effort read of the peer cache. Failure is silent — a
        broken cache file shouldn't prevent the server from starting.
        Records are loaded with their original first_seen but the
        last_seen is reset to "stale" so they enter the retry queue
        on the first sync_once() rather than appearing as fresh peers."""
        try:
            if not self._cache_path.exists():
                return
            import json as _json
            raw = self._cache_path.read_text(encoding="utf-8")
            data = _json.loads(raw)
            if not isinstance(data, list):
                return
            now = time.time()
            for entry in data:
                try:
                    sid = entry.get("server_id")
                    if not sid or sid == get_server_id():
                        continue
                    rec = PeerRecord(
                        server_id=sid,
                        name=str(entry.get("name", "unknown")),
                        host=str(entry.get("host", "")),
                        port=int(entry.get("port", 0)),
                        version=str(entry.get("version", "?")),
                        protocol=str(entry.get("protocol", "http")),
                        users_online=0,
                        uptime=0,
                        # Mark as historically known but stale — the
                        # cluster_mesh retry loop will reprobe on next tick.
                        first_seen=float(entry.get("first_seen", now)),
                        last_seen=now - 3600.0,  # >> stale threshold
                        from_ip=str(entry.get("from_ip", "")),
                    )
                    self._peers[sid] = rec
                except Exception:
                    continue
            logger.info("peer_cache_loaded", count=len(self._peers))
        except Exception as e:
            logger.debug("peer_cache_load_failed", error=str(e))

    def _persist_cache_if_dirty(self) -> None:
        """Write out the current peer table at most once every 30 s.
        Bounded to keep disk I/O off the hot path; stragglers will be
        captured on the next tick."""
        if not self._dirty:
            return
        now = time.time()
        if now - self._last_persist_ts < 30.0:
            return
        try:
            import json as _json
            payload = []
            for rec in self._peers.values():
                payload.append({
                    "server_id": rec.server_id,
                    "name": rec.name,
                    "host": rec.host,
                    "port": rec.port,
                    "version": rec.version,
                    "protocol": rec.protocol,
                    "first_seen": rec.first_seen,
                    "from_ip": rec.from_ip,
                })
            self._cache_path.write_text(
                _json.dumps(payload, indent=0), encoding="utf-8",
            )
            self._dirty = False
            self._last_persist_ts = now
        except Exception as e:
            logger.debug("peer_cache_persist_failed", error=str(e))

    async def ingest(self, data: dict[str, Any], from_ip: str) -> PeerRecord | None:
        """Register/update a peer from a broadcast payload. Returns the record,
        or None if the payload was ours / malformed."""
        try:
            if data.get("type") != "commclient-server":
                return None
            sid = data.get("server_id")
            if not sid or sid == get_server_id():
                return None  # ignore our own broadcast

            now = time.time()
            async with self._lock:
                existing = self._peers.get(sid)
                # Bridge data — peer may advertise multiple host
                # aliases (e.g. when sitting on Ethernet + WiFi at
                # once) and a `bridge: true` flag for cross-subnet
                # relaying.
                aliases_raw = data.get("host_aliases") or []
                if isinstance(aliases_raw, list):
                    host_aliases = [str(x) for x in aliases_raw if isinstance(x, str)]
                else:
                    host_aliases = []
                rec = PeerRecord(
                    server_id=sid,
                    name=str(data.get("name", "unknown")),
                    host=str(data.get("host", from_ip)),
                    port=int(data.get("port", 0)),
                    version=str(data.get("version", "?")),
                    protocol=str(data.get("protocol", "http")),
                    users_online=int(data.get("users_online", 0)),
                    uptime=int(data.get("uptime", 0)),
                    first_seen=existing.first_seen if existing else now,
                    last_seen=now,
                    from_ip=from_ip,
                    bridge=bool(data.get("bridge", False)),
                    host_aliases=host_aliases,
                )
                is_new = existing is None
                self._peers[sid] = rec
                self._dirty = True
            # Throttled disk write — keeps the cache up to date without
            # I/O thrash when broadcasts arrive at high rate.
            self._persist_cache_if_dirty()
            if is_new:
                logger.info(
                    "peer_discovered",
                    server_id=sid,
                    name=rec.name,
                    host=rec.host,
                    port=rec.port,
                    from_ip=from_ip,
                )
            # Mirror into the Kademlia routing table so the DHT layer
            # picks up every peer we learn about, regardless of whether
            # discovery came from UDP, gossip, manual seed, or HTTP
            # find_node response. Lazy import — kademlia depends on
            # discovery_service.get_server_id() which may not yet be
            # initialized when this module first loads in tests.
            try:
                from app.services.dht_kademlia import get_routing_table
                get_routing_table().record_peer(sid, time.time())
            except Exception as _dht_e:
                logger.debug("dht_record_peer_failed",
                             server_id=sid, error=str(_dht_e))

            # Forward to the auto_peer_enrollment service when the
            # broadcast payload carries the new auth fields. Older
            # peers (without nonce/timestamp/signature) are still
            # tracked in this registry but won't enter the approval
            # flow — they'll show as DISCOVERED only if they later
            # send an authenticated /api/federation/peer-announce
            # request. Best-effort + fire-and-forget so the hot
            # broadcast path stays responsive.
            try:
                if all(k in data for k in (
                    "nonce", "timestamp", "signature",
                    "public_key_fingerprint", "cluster_id",
                )):
                    from app.services.auto_peer_enrollment import (
                        auto_peer_enrollment,
                    )
                    _enroll_task = asyncio.create_task(
                        auto_peer_enrollment.handle_discovered_peer({
                            "server_id": sid,
                            "cluster_id": data.get("cluster_id"),
                            "endpoint": (
                                f"{data.get('protocol', 'http')}://"
                                f"{data.get('host') or from_ip}:"
                                f"{data.get('port')}"
                            ) if data.get("port") else None,
                            "region": data.get("region"),
                            "zone": data.get("zone"),
                            "version": data.get("version"),
                            "capabilities": data.get("capabilities") or [],
                            "public_key_fingerprint": data.get(
                                "public_key_fingerprint"
                            ),
                            "discovery_method": "udp_broadcast",
                            "nonce": data.get("nonce"),
                            "timestamp": data.get("timestamp"),
                            "signature": data.get("signature"),
                        })
                    )
                    # Hold a strong reference; otherwise the GC may kill
                    # the task before handle_discovered_peer finishes.
                    self._bg_tasks.add(_enroll_task)
                    _enroll_task.add_done_callback(self._bg_tasks.discard)
            except Exception as _enroll_e:
                logger.debug("peer_enrollment_dispatch_failed",
                             server_id=sid, error=str(_enroll_e))
            return rec
        except Exception as e:
            logger.warning("peer_ingest_failed", error=str(e))
            return None

    async def list(self, include_stale: bool = False) -> list[PeerRecord]:
        async with self._lock:
            peers = list(self._peers.values())
        if not include_stale:
            peers = [p for p in peers if not p.is_stale]
        peers.sort(key=lambda p: p.last_seen, reverse=True)
        return peers

    async def get(self, server_id: str) -> PeerRecord | None:
        async with self._lock:
            return self._peers.get(server_id)

    async def prune_stale(self) -> int:
        """Drop peers we haven't heard from in > PEER_TTL_SECONDS. Returns the
        number removed."""
        now = time.time()
        removed = 0
        async with self._lock:
            dead = [
                sid
                for sid, p in self._peers.items()
                if (now - p.last_seen) > PEER_TTL_SECONDS
            ]
            for sid in dead:
                self._peers.pop(sid, None)
                removed += 1
        if removed:
            logger.info("peer_registry_pruned", count=removed)
        return removed


peer_registry = PeerRegistry()


async def seed_peers_from_env() -> int:
    """Seed the peer registry from ``HELEN_SEED_PEERS`` env var.

    Syntax: ``host1:port1,host2:port2,...`` — each entry is probed via
    ``GET /api/discovery`` to learn the server_id, then inserted.
    Used by the chain-routing test harness where UDP broadcast is
    intentionally disabled (different ports) so each server has to
    learn its adjacent peers out-of-band.

    Retries for up to 30 seconds so a peer that's still booting when we
    first probe gets seeded on a later pass. Returns the number of peers
    eventually seeded.
    """
    import os as _os
    raw = (_os.environ.get("HELEN_SEED_PEERS") or "").strip()
    if not raw:
        return 0

    entries = [
        (host.strip(), int(port_s))
        for host, _, port_s in (e.strip().partition(":") for e in raw.split(",") if e.strip())
        if host and port_s.isdigit()
    ]
    if not entries:
        return 0

    import asyncio as _asyncio
    import httpx as _httpx

    seeded_ids: set[str] = set()

    async def _probe_once(client: _httpx.AsyncClient, host: str, port: int) -> bool:
        try:
            r = await client.get(f"http://{host}:{port}/api/discovery")
            if r.status_code != 200:
                return False
            data = r.json()
        except Exception:
            return False
        rec = await peer_registry.ingest(data, from_ip=host)
        if rec is None:
            return False
        seeded_ids.add(rec.server_id)
        logger.info("peer_seeded_from_env",
                    host=host, port=port,
                    server_id=rec.server_id, name=rec.name)
        return True

    # Perpetual refresher. Seeded peers don't get UDP broadcasts (the
    # whole reason we're seeding manually is broadcast-blocked topologies)
    # so without periodic re-probing, ``last_seen`` ages out past
    # ``PEER_TTL_SECONDS`` and the registry marks them stale —
    # ``federation_service.emit_to_remote_user`` then refuses to use
    # them and chain forwards silently fail. Re-probe every 10s so the
    # registry's recency timestamp stays fresh.
    refresh_interval = 10.0

    async def _seed_loop():
        async with _httpx.AsyncClient(timeout=3.0) as client:
            # Initial burst: try every seed once a second until all are
            # reachable, then drop to refresh cadence.
            for _ in range(30):
                ok_now = 0
                for host, port in entries:
                    if await _probe_once(client, host, port):
                        ok_now += 1
                if ok_now >= len(entries):
                    break
                await _asyncio.sleep(1.0)
            # Steady-state refresher.
            while True:
                await _asyncio.sleep(refresh_interval)
                for host, port in entries:
                    try:
                        await _probe_once(client, host, port)
                    except Exception as _e:
                        logger.debug("peer_seed_refresh_fail",
                                     host=host, port=port, error=str(_e))

    # Hold the seed-loop task on the singleton registry so it survives
    # GC. The task runs forever (steady-state refresher); cancel it on
    # shutdown via peer_registry._bg_tasks if needed.
    _seed_task = _asyncio.create_task(_seed_loop())
    peer_registry._bg_tasks.add(_seed_task)
    _seed_task.add_done_callback(peer_registry._bg_tasks.discard)

    # One initial synchronous sweep so tests that come up fully-booted in
    # order see the seed immediately; the background task picks up stragglers.
    async with _httpx.AsyncClient(timeout=3.0) as client:
        for host, port in entries:
            await _probe_once(client, host, port)

    return len(seeded_ids)


# ── UDP listener ─────────────────────────────────────────────


class UDPListenerService:
    """
    Listen on DISCOVERY_UDP_PORT for peer broadcasts and feed the registry.

    Uses SO_REUSEADDR (+ SO_REUSEPORT on platforms that support it) so multiple
    server instances on the same host can all listen. Binding/receive errors
    are non-fatal — the service logs and backs off, because this is a
    best-effort discovery channel, not a required dependency.
    """

    def __init__(self) -> None:
        self._running = False
        self._rx_task: asyncio.Task | None = None
        self._prune_task: asyncio.Task | None = None
        self._sock: socket.socket | None = None
        self._aux_socks: list[socket.socket] = []
        self._aux_rx_tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # SO_REUSEPORT is Linux/macOS only; Windows uses SO_REUSEADDR alone.
            if hasattr(socket, "SO_REUSEPORT") and sys.platform != "win32":
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("0.0.0.0", settings.DISCOVERY_UDP_PORT))
            sock.setblocking(False)
            self._sock = sock
        except OSError as e:
            logger.warning(
                "udp_listener_bind_failed",
                port=settings.DISCOVERY_UDP_PORT,
                error=str(e),
                hint="peer discovery will be send-only on this instance",
            )
            self._running = False
            return

        # Multi-port listener — bind aux sockets on backup ports so
        # broadcast still works when a corporate firewall happens to
        # have blocked the primary 41234. Failures here are non-fatal:
        # the primary socket already gives us baseline coverage. Ports
        # are commclient-prefixed and configurable via env.
        import os as _os_mp
        aux_ports_str = _os_mp.environ.get(
            "HELEN_DISCOVERY_AUX_PORTS", "41235,41236,41237"
        )
        for raw in aux_ports_str.split(","):
            try:
                port = int(raw.strip())
                if port == settings.DISCOVERY_UDP_PORT:
                    continue
                aux = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                aux.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                aux.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                aux.bind(("0.0.0.0", port))
                aux.setblocking(False)
                self._aux_socks.append(aux)
                self._aux_rx_tasks.append(
                    asyncio.create_task(self._aux_recv_loop(aux, port))
                )
            except (OSError, ValueError) as _aux_e:
                # Port in use or out of range — skip silently. We have
                # the primary listener; aux ports are belt-and-braces.
                logger.debug("udp_aux_listener_skip", port=raw, error=str(_aux_e))

        self._rx_task = asyncio.create_task(self._recv_loop())
        self._prune_task = asyncio.create_task(self._prune_loop())
        logger.info(
            "udp_listener_started",
            port=settings.DISCOVERY_UDP_PORT,
            aux_ports=[s.getsockname()[1] for s in self._aux_socks],
        )

    async def stop(self) -> None:
        self._running = False
        all_tasks = [self._rx_task, self._prune_task] + list(self._aux_rx_tasks)
        for t in all_tasks:
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        for s in self._aux_socks:
            try:
                s.close()
            except Exception:
                pass
        self._aux_socks.clear()
        self._aux_rx_tasks.clear()
        logger.info("udp_listener_stopped")

    async def _recv_loop(self) -> None:
        assert self._sock is not None
        await self._consume_from(self._sock)

    async def _aux_recv_loop(self, sock: socket.socket, port: int) -> None:
        """Mirror of `_recv_loop` for an aux port. Same ingest path —
        a peer broadcasting on either the primary or an aux port lands
        in the same registry."""
        try:
            await self._consume_from(sock)
        except Exception as e:
            logger.debug("udp_aux_recv_loop_exit", port=port, error=str(e))

    async def _consume_from(self, sock: socket.socket) -> None:
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                data, addr = await loop.sock_recvfrom(sock, 4096)
            except (BlockingIOError, InterruptedError):
                await asyncio.sleep(0.05)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("udp_listener_recv_error", error=str(e))
                await asyncio.sleep(0.2)
                continue

            try:
                payload = json.loads(data.decode("utf-8", errors="replace"))
            except Exception:
                continue
            await peer_registry.ingest(payload, from_ip=addr[0])

    async def _prune_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(max(5, settings.DISCOVERY_BROADCAST_INTERVAL * 2))
                await peer_registry.prune_stale()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("udp_listener_prune_error", error=str(e))


udp_listener_service = UDPListenerService()
