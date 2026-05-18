"""
UDP multi-hop relay.

Each Helen server can act as a transparent hop in a chain of relays. A
single `RelaySession` owns one ephemeral UDP port; any packet that lands
on that port is forwarded verbatim to the configured `next_hop`, and any
packet that comes back from the peer we last talked to on the next_hop
socket is forwarded back to the last source that hit the ingress port.

This is deliberately thin: no TURN framing, no auth, no RTP parsing.
Just memcpy + sendto. One hop costs <1% CPU per active call.

```
 prev_hop ─► ingress:port_in  ╲  ╱  egress ─► next_hop.host:next_hop.port
                                XX
 prev_hop ◄─ ingress:port_in  ╱  ╲  egress ◄─ next_hop
```

Sessions expire after `idle_ttl_seconds` of inactivity so a dropped call
doesn't leak a port forever. A background janitor sweeps every 15s.

Used by:
  * `/api/federation/relay/alloc`   — peers wire up a chain hop-by-hop
  * `/api/calls/federated/ice`      — client entry point (first hop)
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_IDLE_TTL = 180.0  # seconds of no traffic before we reap a session
JANITOR_INTERVAL = 15.0   # how often the reaper runs


class RelayQuotaExceeded(Exception):
    """Raised when a relay allocation would blow the global or per-peer cap."""


class RelayRateLimited(Exception):
    """Raised when a peer exceeds the per-peer alloc rate."""


class _TokenBucket:
    """Leaky token bucket. `rate` tokens/sec, `capacity` max burst."""
    __slots__ = ("rate", "capacity", "tokens", "last")

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last = time.monotonic()

    def consume(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(
            self.capacity, self.tokens + (now - self.last) * self.rate,
        )
        self.last = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class RelayAllocRateLimiter:
    """Per-peer token-bucket rate limiter for relay allocation calls."""

    def __init__(self) -> None:
        self._buckets: dict[str, _TokenBucket] = {}

    def check(self, peer_key: str) -> bool:
        """Return True if allowed, False if rate-limited."""
        from app.core.config import get_settings
        s = get_settings()
        rate = s.FEDERATION_RELAY_ALLOC_RATE_PER_SEC
        burst = s.FEDERATION_RELAY_ALLOC_BURST
        if rate <= 0 or burst <= 0:
            return True  # disabled
        key = peer_key or "_anon"
        b = self._buckets.get(key)
        if b is None or b.rate != rate or b.capacity != burst:
            b = _TokenBucket(rate, burst)
            self._buckets[key] = b
        return b.consume(1.0)


relay_alloc_rate_limiter = RelayAllocRateLimiter()


@dataclass
class RelaySession:
    """A single forwarder, one port in and one socket out."""
    relay_id: str
    ingress_host: str
    ingress_port: int
    next_hop_host: str
    next_hop_port: int
    idle_ttl: float = DEFAULT_IDLE_TTL
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    bytes_forwarded: int = 0
    packets_forwarded: int = 0
    # Peer (origin server_id) that requested this session. "" for local
    # allocations (e.g. CLI tools or tests). Used for per-peer quota
    # accounting.
    owner_peer: str = ""

    # Populated at start()
    _ingress_sock: socket.socket | None = None
    _egress_sock: socket.socket | None = None
    _last_prev_hop: tuple[str, int] | None = None  # last src on ingress side
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _closed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "relay_id": self.relay_id,
            "ingress_host": self.ingress_host,
            "ingress_port": self.ingress_port,
            "next_hop_host": self.next_hop_host,
            "next_hop_port": self.next_hop_port,
            "age_seconds": round(time.time() - self.created_at, 2),
            "idle_seconds": round(time.time() - self.last_activity, 2),
            "bytes_forwarded": self.bytes_forwarded,
            "packets_forwarded": self.packets_forwarded,
            "owner_peer": self.owner_peer,
        }

    @property
    def is_idle_expired(self) -> bool:
        return (time.time() - self.last_activity) > self.idle_ttl


class RelayManager:
    """
    Owns all active `RelaySession`s on this server. Thread-safe via the
    GIL + asyncio single-threaded event loop assumption — sessions live
    on the same loop they were created on.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, RelaySession] = {}
        self._janitor_task: asyncio.Task | None = None
        self._bind_host = "0.0.0.0"

    async def start(self, bind_host: str = "0.0.0.0") -> None:
        self._bind_host = bind_host
        if self._janitor_task is None or self._janitor_task.done():
            self._janitor_task = asyncio.create_task(self._janitor_loop())
            logger.info("relay_manager_started", bind_host=bind_host)

    async def stop(self) -> None:
        if self._janitor_task is not None:
            self._janitor_task.cancel()
            try:
                await self._janitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._janitor_task = None
        for rid in list(self._sessions.keys()):
            await self._close_session(rid)
        logger.info("relay_manager_stopped")

    def count_for_peer(self, owner_peer: str) -> int:
        """How many active sessions a given peer currently owns."""
        if not owner_peer:
            return 0
        return sum(
            1 for s in self._sessions.values() if s.owner_peer == owner_peer
        )

    def session_count(self) -> int:
        return len(self._sessions)

    async def allocate(
        self,
        next_hop_host: str,
        next_hop_port: int,
        idle_ttl: float = DEFAULT_IDLE_TTL,
        owner_peer: str = "",
    ) -> RelaySession:
        """Open a new ingress port and start forwarding toward next_hop.

        Raises `RelayQuotaExceeded` when either the global cap or the
        per-peer quota would be exceeded.
        """
        from app.core.config import get_settings
        s = get_settings()
        if len(self._sessions) >= s.FEDERATION_MAX_RELAY_SESSIONS:
            raise RelayQuotaExceeded(
                f"global relay cap reached ({s.FEDERATION_MAX_RELAY_SESSIONS})"
            )
        if owner_peer and (
            self.count_for_peer(owner_peer) >= s.FEDERATION_PER_PEER_RELAY_QUOTA
        ):
            raise RelayQuotaExceeded(
                f"per-peer quota reached for {owner_peer} "
                f"({s.FEDERATION_PER_PEER_RELAY_QUOTA})"
            )

        # Bind to an ephemeral port chosen by the OS.
        ingress = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ingress.setblocking(False)
        ingress.bind((self._bind_host, 0))
        bound_host, bound_port = ingress.getsockname()

        egress = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        egress.setblocking(False)
        # Explicit bind to ephemeral port — REQUIRED on Windows Proactor so
        # the reverse-path recvfrom IOCP handle has a local address the OS
        # can deliver inbound datagrams to. Without this, the socket picks
        # its local port lazily on first sendto, but by then the pending
        # recvfrom was issued against an unbound handle and silently never
        # completes.
        egress.bind((self._bind_host, 0))

        rid = secrets.token_hex(8)
        session = RelaySession(
            relay_id=rid,
            ingress_host=bound_host,
            ingress_port=bound_port,
            next_hop_host=next_hop_host,
            next_hop_port=next_hop_port,
            idle_ttl=idle_ttl,
            owner_peer=owner_peer,
            _ingress_sock=ingress,
            _egress_sock=egress,
        )
        self._sessions[rid] = session

        # Two unidirectional pumps: ingress→egress and egress→ingress.
        # Wrapped in a supervisor that restarts on unexpected exceptions
        # (up to MAX_PUMP_RESTARTS) so a transient OS error doesn't leave
        # the session alive-but-deaf.
        session._tasks = [
            asyncio.create_task(
                self._supervised_pump(session, self._pump_ingress, "ingress")
            ),
            asyncio.create_task(
                self._supervised_pump(session, self._pump_egress, "egress")
            ),
        ]
        logger.info(
            "relay_allocated",
            relay_id=rid,
            ingress_port=bound_port,
            next_hop=f"{next_hop_host}:{next_hop_port}",
        )
        return session

    async def release(self, relay_id: str) -> bool:
        return await self._close_session(relay_id)

    def get(self, relay_id: str) -> RelaySession | None:
        return self._sessions.get(relay_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self._sessions.values()]

    # ── Internals ──────────────────────────────────────────

    MAX_PUMP_RESTARTS = 3

    async def _supervised_pump(
        self,
        s: RelaySession,
        pump_fn,
        direction: str,
    ) -> None:
        """Run a pump; restart on unexpected exception, up to a cap."""
        restarts = 0
        while not s._closed:
            try:
                await pump_fn(s)
                return  # clean exit (session closed)
            except asyncio.CancelledError:
                return
            except Exception as e:
                restarts += 1
                logger.warning(
                    "relay_pump_crashed",
                    relay_id=s.relay_id, direction=direction,
                    restarts=restarts, error=str(e),
                )
                if restarts > self.MAX_PUMP_RESTARTS:
                    logger.error(
                        "relay_pump_giving_up",
                        relay_id=s.relay_id, direction=direction,
                    )
                    # Close the session so the peer notices via janitor
                    # rather than silently eating traffic.
                    asyncio.create_task(self._close_session(s.relay_id))
                    return
                # Short back-off so we don't hot-loop a broken socket.
                await asyncio.sleep(0.1 * restarts)

    async def _pump_ingress(self, s: RelaySession) -> None:
        """prev_hop → next_hop direction."""
        assert s._ingress_sock is not None and s._egress_sock is not None
        loop = asyncio.get_event_loop()
        sock = s._ingress_sock
        try:
            while not s._closed:
                try:
                    data, src = await loop.sock_recvfrom(sock, 2048)
                except (BlockingIOError, InterruptedError):
                    await asyncio.sleep(0.005)
                    continue
                except asyncio.CancelledError:
                    break
                except OSError:
                    break
                s._last_prev_hop = src
                s.last_activity = time.time()
                s.bytes_forwarded += len(data)
                s.packets_forwarded += 1
                try:
                    await loop.sock_sendto(
                        s._egress_sock, data,
                        (s.next_hop_host, s.next_hop_port),
                    )
                except OSError:
                    # Peer unreachable — keep the session alive; traffic may
                    # return when the peer comes back.
                    pass
        finally:
            pass

    async def _pump_egress(self, s: RelaySession) -> None:
        """next_hop → prev_hop direction (return path)."""
        assert s._ingress_sock is not None and s._egress_sock is not None
        loop = asyncio.get_event_loop()
        sock = s._egress_sock
        try:
            while not s._closed:
                try:
                    data, _src = await loop.sock_recvfrom(sock, 2048)
                except (BlockingIOError, InterruptedError):
                    await asyncio.sleep(0.005)
                    continue
                except asyncio.CancelledError:
                    break
                except OSError:
                    break
                s.last_activity = time.time()
                s.bytes_forwarded += len(data)
                s.packets_forwarded += 1
                prev = s._last_prev_hop
                if prev is None:
                    # Nothing to return to yet.
                    continue
                try:
                    await loop.sock_sendto(s._ingress_sock, data, prev)
                except OSError:
                    pass
        finally:
            pass

    async def _close_session(self, rid: str) -> bool:
        s = self._sessions.pop(rid, None)
        if s is None:
            return False
        s._closed = True
        for t in s._tasks:
            t.cancel()
        for t in s._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for sock in (s._ingress_sock, s._egress_sock):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
        logger.info(
            "relay_released",
            relay_id=rid,
            bytes_forwarded=s.bytes_forwarded,
            packets_forwarded=s.packets_forwarded,
        )
        return True

    async def _janitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(JANITOR_INTERVAL)
                dead = [rid for rid, s in self._sessions.items() if s.is_idle_expired]
                for rid in dead:
                    await self._close_session(rid)
                if dead:
                    logger.info("relay_janitor_reaped", count=len(dead))
                    try:
                        from app.services.federation_metrics import incr
                        incr("relay_janitor_reaped", len(dead))
                    except Exception:
                        pass
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("relay_janitor_error", error=str(e))


relay_manager = RelayManager()
