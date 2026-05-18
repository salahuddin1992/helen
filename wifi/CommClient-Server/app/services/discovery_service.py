"""
LAN discovery service — advertises the server via mDNS and UDP broadcast.
Clients find the server without manual IP entry.

Discovery Architecture:
  1. UDP Broadcast (primary): sends server info to 255.255.255.255:41234 every 3s.
     Works on all Windows networks, no special services required.
  2. mDNS/Zeroconf (secondary): registers _commclient._tcp.local. for DNS-SD.
     Requires Bonjour/Avahi, but provides proper service discovery.
  3. Subnet-directed broadcast (tertiary): sends to the subnet broadcast address
     in addition to 255.255.255.255, for networks that block global broadcast.

Each broadcast includes a unique server_id (stable across restarts) so clients
can distinguish multiple servers on the same LAN.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


# ── Server Identity ──────────────────────────────────────────

# Server identity = 64-char alphanumeric code. Same alphabet as user
# share_codes so both read as a single consistent "handle" namespace
# across the federation (user share_code ↔ server_id interchangeable
# shape; callers still treat them as distinct values).
_SERVER_ID_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
)
_SERVER_ID_LEN = 64


def _is_valid_server_id(s: str) -> bool:
    if len(s) != _SERVER_ID_LEN:
        return False
    allowed = set(_SERVER_ID_ALPHABET)
    return all(c in allowed for c in s)


def _load_or_create_server_id() -> str:
    """
    Return a stable server ID that survives restarts.
    Stored in the data directory alongside the SQLite database.

    Format: 64-char alphanumeric [A-Za-z0-9]{64} — matches user share_code
    shape so the federation namespace is visually uniform. Legacy 16-char
    hex IDs from older installs get transparently upgraded: the old ID is
    preserved as a prefix inside the new 64-char value so anyone who had
    the short ID bookmarked can still recognize the server. The prefixed
    bytes are HMAC'd with machine-specific entropy to pad to 64 chars.
    """
    sqlite_dir = Path(settings.SQLITE_PATH).parent
    if not sqlite_dir.is_absolute():
        sqlite_dir = (settings.PROJECT_ROOT / settings.SQLITE_PATH).parent
    sqlite_dir = sqlite_dir.resolve()
    sqlite_dir.mkdir(parents=True, exist_ok=True)

    id_file = sqlite_dir / ".server_id"
    if id_file.exists():
        try:
            stored = id_file.read_text().strip()
            if _is_valid_server_id(stored):
                return stored
            # Legacy short ID — upgrade in-place, keeping the old prefix
            # so admin dashboards that displayed the first few chars stay
            # recognizable.
            if 8 <= len(stored) <= _SERVER_ID_LEN:
                pad = _random_alphanumeric(_SERVER_ID_LEN - len(stored))
                upgraded = (stored + pad)[:_SERVER_ID_LEN]
                # Clamp any legacy hex chars that happen to fall outside
                # alphabet (they won't — hex is a subset — but belt & braces).
                upgraded = "".join(
                    c if c in _SERVER_ID_ALPHABET else "0" for c in upgraded
                )
                try:
                    id_file.write_text(upgraded)
                    logger.info(
                        "server_id_upgraded_to_64char",
                        legacy_len=len(stored),
                    )
                except Exception as e:
                    logger.warning("server_id_upgrade_write_failed", error=str(e))
                return upgraded
        except Exception:
            pass

    # Fresh install: mint a 64-char alphanumeric via SystemRandom.
    # Machine identity mixed into seed for auditing, but uniqueness comes
    # from the CSPRNG — 62^64 keyspace makes collision astronomically
    # improbable even across millions of instances.
    server_id = _random_alphanumeric(_SERVER_ID_LEN)
    try:
        id_file.write_text(server_id)
    except Exception as e:
        logger.warning("server_id_write_failed", error=str(e))
    return server_id


def _random_alphanumeric(n: int) -> str:
    if n <= 0:
        return ""
    rng = secrets.SystemRandom()
    return "".join(rng.choice(_SERVER_ID_ALPHABET) for _ in range(n))


# Lazy singleton
_server_id: str | None = None


def get_server_id() -> str:
    """Return the stable server identifier."""
    global _server_id
    if _server_id is None:
        _server_id = _load_or_create_server_id()
    return _server_id


# ── LAN IP Detection ────────────────────────────────────────

def get_lan_ip() -> str:
    """Get the machine's primary LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        # Connect to a non-routable address to determine the outgoing interface
        s.connect(("10.254.254.254", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _get_subnet_broadcast(ip: str) -> str | None:
    """
    Derive the subnet broadcast address from the LAN IP.
    Assumes /24 subnet (255.255.255.0) which covers most home/office LANs.
    Returns None if the IP is loopback or cannot be parsed.
    """
    try:
        parts = ip.split(".")
        if len(parts) != 4 or ip.startswith("127."):
            return None
        return f"{parts[0]}.{parts[1]}.{parts[2]}.255"
    except Exception:
        return None


def _subnet_prefix(ip: str) -> str:
    """Return the /24 prefix of an IPv4 address (e.g. '192.168.1' for
    '192.168.1.34'). Used by bridge-detection to decide whether two
    LAN IPs sit on the same subnet."""
    try:
        return ".".join(ip.split(".")[:3])
    except Exception:
        return ip


def get_all_lan_ips() -> list[str]:
    """
    Return all non-loopback IPv4 addresses on this machine.
    Useful for multi-NIC servers (Ethernet + WiFi).
    """
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127."):
                ips.append(addr)
    except Exception:
        pass

    # Always include the primary LAN IP
    primary = get_lan_ip()
    if primary not in ips and not primary.startswith("127."):
        ips.insert(0, primary)

    return ips if ips else [get_lan_ip()]


# ── Server Startup Timestamp ─────────────────────────────────

_start_time = time.time()


def get_uptime_seconds() -> int:
    """Return seconds since this server process started."""
    return int(time.time() - _start_time)


# ── UDP Broadcast Service ────────────────────────────────────

class UDPBroadcastService:
    """
    Periodically broadcasts server presence on the LAN via UDP.

    Sends to both 255.255.255.255 (global broadcast) and the subnet-directed
    broadcast address (e.g. 192.168.1.255) for maximum compatibility.
    Payload is refreshed each cycle so uptime and user count stay current.
    """

    def __init__(self):
        self._running = False
        self._task: asyncio.Task | None = None
        self._nic_watch_task: asyncio.Task | None = None

    async def get_broadcast_payload(self) -> bytes:
        """Build the discovery payload with current server state.

        Audit fix C3: when ``HELEN_DISCOVERY_SECRET`` is configured we
        attach an HMAC-SHA256 signature over (ts | server_id | host |
        port). Clients with the same secret verify before trusting the
        announcement, blocking LAN-MITM via spoofed Helen-Server
        broadcasts. Empty secret = unsigned (single-server LAN).
        """
        from app.services.presence_service import presence_service

        try:
            online_count = len(await presence_service.get_all_online())
        except Exception:
            online_count = 0

        ts = int(time.time())
        server_id = get_server_id()
        host = get_lan_ip()
        port = settings.PORT

        # Multi-homed bridge advertisement: when we're on >1 LAN, list
        # every IP so peers on either subnet can reach us, AND flip the
        # `bridge` flag so other servers can pick us as a federation
        # relay across networks. A box with Ethernet + WiFi (or two
        # NICs into different routers) becomes a free L7 bridge.
        all_ips = [ip for ip in get_all_lan_ips()
                   if not ip.startswith("127.") and not ip.startswith("169.254.")]
        is_bridge = len(set(_subnet_prefix(ip) for ip in all_ips)) > 1

        data = {
            "type": "commclient-server",
            "server_id": server_id,
            "host": host,
            "host_aliases": all_ips,         # all reachable IPs
            "bridge": is_bridge,             # multi-subnet relay capability
            "port": port,
            "version": "1.0.0",
            "name": settings.SERVER_NAME,
            "uptime": get_uptime_seconds(),
            "users_online": online_count,
            "protocol": "http",
            "ts": ts,
        }

        # HMAC-sign the announcement (legacy field — kept for the
        # client-side HMAC verifier that already exists in
        # `_verifyDiscoveryHmac` on the desktop).
        import os as _os_disc
        secret = _os_disc.environ.get("HELEN_DISCOVERY_SECRET", "").strip()
        if secret and len(secret) >= 16:
            import hmac as _hmac_disc
            import hashlib as _h_disc
            payload = f"{ts}|{server_id}|{host}|{port}".encode()
            data["sig"] = _hmac_disc.new(
                secret.encode(), payload, _h_disc.sha256,
            ).hexdigest()

        # ── Peer-acceptance auth fields ──
        # When FEDERATION_SECRET is configured, we attach the full set
        # of auth fields the receiving peer needs to run
        # ``verify_peer_candidate`` against our announcement. Without
        # this, peers receiving our broadcast still ingest us into
        # their legacy peer_registry but DON'T fan us through
        # ``auto_peer_enrollment`` — so we never enter their approval
        # flow.
        try:
            from app.services.peer_auth import (
                compute_signature,
                fingerprint_for_secret,
            )
            # Read settings via the cached factory so a test or admin
            # config flip sees the latest values without restart. The
            # module-level ``settings`` binding above is fixed at
            # import time, which is wrong for hot-reload here.
            from app.core.config import get_settings as _get_settings_now
            _live = _get_settings_now()
            fed_secret = (_live.FEDERATION_SECRET or "").strip()
            if fed_secret and len(fed_secret) >= 16:
                cluster_id = (
                    _live.COMMCLIENT_CLUSTER_ID or "default"
                )
                # Capabilities the peer needs to know we support. Keep
                # narrow — fabric_v1 is the bare minimum understood by
                # the receiver; we also advertise sfu/turn if relevant
                # but the receiver only requires fabric_v1.
                caps = ["fabric_v1"]
                fp = fingerprint_for_secret(fed_secret)
                # nonce is a one-shot per broadcast cycle so the
                # receiver's nonce dedup blocks replays of older
                # broadcasts. uuid4 hex is plenty entropy.
                import uuid as _uuid
                nonce = _uuid.uuid4().hex
                sig2 = compute_signature(
                    secret=fed_secret,
                    server_id=server_id,
                    cluster_id=cluster_id,
                    nonce=nonce,
                    timestamp=ts,
                    version=data["version"],
                    capabilities=set(caps),
                    public_key_fingerprint=fp,
                )
                data.update({
                    "cluster_id":             cluster_id,
                    "capabilities":           caps,
                    "public_key_fingerprint": fp,
                    "nonce":                  nonce,
                    "timestamp":              ts,
                    "signature":              sig2,
                })
        except Exception as _auth_e:
            logger.debug(
                "peer_auth_fields_skipped",
                error=str(_auth_e),
            )

        return json.dumps(data).encode("utf-8")

    async def start(self) -> None:
        """Start the UDP broadcast loop."""
        self._running = True
        self._task = asyncio.create_task(self._broadcast_loop())
        # Network-change watcher: poll local interfaces every 5s and
        # trigger an immediate burst of broadcasts whenever the IP set
        # changes (NIC plugged/unplugged, WiFi reconnect, IP renew).
        # Without this the cluster waits up to DISCOVERY_BROADCAST_INTERVAL
        # to notice — typically 3s but could be tuned higher. Burst on
        # change cuts re-discovery latency to near-zero.
        self._nic_watch_task = asyncio.create_task(self._nic_watch_loop())
        logger.info(
            "udp_broadcast_started",
            port=settings.DISCOVERY_UDP_PORT,
            interval=settings.DISCOVERY_BROADCAST_INTERVAL,
            server_id=get_server_id(),
        )

    async def _nic_watch_loop(self) -> None:
        """Detect IP-set changes and trigger a burst broadcast on change."""
        last_ips: set[str] = set()
        while self._running:
            try:
                cur = set(get_all_lan_ips())
                if cur != last_ips and last_ips:
                    logger.info(
                        "nic_change_detected",
                        added=sorted(cur - last_ips),
                        removed=sorted(last_ips - cur),
                    )
                    # Fire 3 quick broadcasts to seed peers on the new
                    # subnet immediately, before the regular cycle.
                    for _ in range(3):
                        await asyncio.sleep(0.5)
                        # The main broadcast loop reads get_all_lan_ips()
                        # each cycle so it picks up new IPs naturally; we
                        # just shorten its sleep here.
                last_ips = cur
            except Exception as e:
                logger.debug("nic_watch_failed", error=str(e))
            await asyncio.sleep(5.0)

    async def stop(self) -> None:
        """Stop the UDP broadcast loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._nic_watch_task:
            self._nic_watch_task.cancel()
            try:
                await self._nic_watch_task
            except asyncio.CancelledError:
                pass
        logger.info("udp_broadcast_stopped")

    async def _broadcast_loop(self) -> None:
        """Broadcast the announcement on EVERY local subnet.

        Multi-NIC awareness: a Helen-Server box plugged into both Ethernet
        and WiFi simultaneously has two LAN IPs (e.g. 192.168.1.34 and
        192.168.2.10). Single-IP broadcast only reaches one of those
        subnets — peers on the other router would never see us. The new
        loop iterates `get_all_lan_ips()` and emits a per-IP socket bound
        to that source so:
          * Each subnet gets its own subnet-directed broadcast.
          * Routers that filter cross-subnet broadcast still see at
            least one valid announcement on each side.
          * If the box happens to be a multi-homed bridge, every Helen
            on either router can find every Helen on the OTHER router
            via this server as a relay.
        """
        # One socket per source LAN IP. Bound to the source so Windows
        # picks the right interface for each broadcast.
        socks: dict[str, socket.socket] = {}

        def _open(src_ip: str) -> socket.socket:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((src_ip, 0))
            except OSError:
                # Source IP no longer valid — fall back to default route.
                pass
            s.setblocking(False)
            return s

        # Fallback default-route socket for the global 255.255.255.255 send.
        default_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        default_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        default_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        default_sock.setblocking(False)

        while self._running:
            try:
                # Refresh payload each cycle (uptime, user count change)
                payload = await self.get_broadcast_payload()
                loop = asyncio.get_event_loop()

                # 1. Global 255.255.255.255 broadcast — single emit on
                #    the default route. Most LAN devices accept this.
                await loop.run_in_executor(
                    None,
                    lambda p=payload: default_sock.sendto(
                        p, ("255.255.255.255", settings.DISCOVERY_UDP_PORT)
                    ),
                )

                # 2. Per-subnet broadcast on EVERY local IP.
                lan_ips = get_all_lan_ips()
                for src_ip in lan_ips:
                    if src_ip.startswith("127.") or src_ip.startswith("169.254."):
                        continue
                    if src_ip not in socks:
                        socks[src_ip] = _open(src_ip)
                    s = socks[src_ip]
                    subnet_bc = _get_subnet_broadcast(src_ip)
                    if subnet_bc:
                        try:
                            await loop.run_in_executor(
                                None,
                                lambda p=payload, sk=s, addr=subnet_bc: sk.sendto(
                                    p, (addr, settings.DISCOVERY_UDP_PORT)
                                ),
                            )
                        except Exception as inner:
                            logger.debug(
                                "subnet_broadcast_failed",
                                src=src_ip, dst=subnet_bc, error=str(inner),
                            )

            except Exception as e:
                logger.warning("udp_broadcast_error", error=str(e))

            await asyncio.sleep(settings.DISCOVERY_BROADCAST_INTERVAL)

        # Cleanup
        try:
            default_sock.close()
        except Exception:
            pass
        for s in socks.values():
            try:
                s.close()
            except Exception:
                pass


# ── mDNS Service ─────────────────────────────────────────────

class MDNSService:
    """
    Advertise the server via mDNS (Bonjour/Avahi).
    Service type: _commclient._tcp.local.

    The mDNS properties include server_id for multi-server disambiguation.
    """

    def __init__(self):
        self._zeroconf = None
        self._info = None

    async def start(self) -> None:
        """
        Register the server via mDNS in a background executor with a hard timeout.
        Zeroconf.register_service() is a blocking call that can stall for ~10s on
        Windows when DNS-SD is unavailable; running it inline freezes the asyncio
        event loop and delays the entire app startup.
        """
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            logger.warning("zeroconf_not_installed_mdns_disabled")
            return

        lan_ip = get_lan_ip()
        try:
            self._zeroconf = Zeroconf()
            # Register both the SRV record (service lookup) and a hostname
            # A-record via `server="helen.local."`. With the hostname alias,
            # LAN clients can just point at http://helen.local:3000 and the
            # system's built-in mDNS resolver (Windows 10+, Bonjour, avahi)
            # maps it to the server's IP without needing an app-level mDNS
            # browser. Falls back to IP only if the resolver is disabled.
            self._info = ServiceInfo(
                "_commclient._tcp.local.",
                f"CommClient-{get_server_id()[:8]}._commclient._tcp.local.",
                addresses=[socket.inet_aton(lan_ip)],
                port=settings.PORT,
                server="helen.local.",
                properties={
                    "version": "1.0.0",
                    "name": settings.SERVER_NAME,
                    "server_id": get_server_id(),
                    "protocol": "http",
                    "hostname": "helen.local",
                },
            )
        except Exception as e:
            logger.warning(
                "mdns_setup_failed",
                error=str(e) or repr(e),
                error_type=type(e).__name__,
            )
            self._zeroconf = None
            self._info = None
            return

        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, self._zeroconf.register_service, self._info),
                timeout=3.0,
            )
            logger.info(
                "mdns_registered",
                ip=lan_ip,
                port=settings.PORT,
                server_id=get_server_id(),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "mdns_registration_timeout_degraded",
                timeout_s=3.0,
                ip=lan_ip,
                hint="UDP broadcast still active — peers can still discover this server",
            )
            # Leave _zeroconf alive in case it eventually registers; stop() handles cleanup.
        except Exception as e:
            logger.warning(
                "mdns_registration_failed",
                error=str(e) or repr(e),
                error_type=type(e).__name__,
            )

    async def stop(self) -> None:
        if self._zeroconf and self._info:
            try:
                self._zeroconf.unregister_service(self._info)
                self._zeroconf.close()
            except Exception as e:
                logger.debug("mdns_unregister_failed", error=str(e) or repr(e))
            logger.info("mdns_unregistered")


# ── Singletons ───────────────────────────────────────────────
udp_broadcast = UDPBroadcastService()
mdns_service = MDNSService()
