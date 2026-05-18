"""
LAN-aware ICE helper (Task #3).

Extends the existing `app.services.ice_config_service` WITHOUT modifying it.

Why
---
`ice_config_service._detected_lan_ip()` picks a single LAN IP via the
`UDP connect()` trick. On a multi-homed server (Wi-Fi + Ethernet +
virtual adapters from VMware/WSL/Hyper-V) that can be the wrong one —
e.g. the `vEthernet (WSL)` 172.28.x.x address which isn't reachable from
other physical clients on the LAN. The result is that every remote client
sees host candidates it cannot route to, and has to fall back to TURN
relay (or worse, fail outright).

This module:
  * Enumerates every IPv4 interface.
  * Filters to real private-LAN ranges (RFC 1918 + CGNAT + link-local).
  * Ranks them using a simple heuristic (physical > wireless > virtual).
  * Exposes a list of "announce-worthy" IPs for ICE SDP injection.
  * Exposes a list of allowed LAN origins for dynamic CORS.
  * Pure read-only — the existing ice_config_service keeps working
    exactly as before for callers that don't opt in.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

try:
    import psutil  # type: ignore
    _HAVE_PSUTIL = True
except Exception:  # pragma: no cover — psutil is optional
    _HAVE_PSUTIL = False


# ─────────────────────────────────────────────────────────────────────────────
# Virtual-adapter denylist (substring match, case-insensitive)
# ─────────────────────────────────────────────────────────────────────────────

_VIRTUAL_NAME_HINTS: tuple[str, ...] = (
    "vethernet",
    "vmware",
    "virtualbox",
    "vbox",
    "hyper-v",
    "docker",
    "br-",
    "wsl",
    "loopback",
    "bluetooth",
    "teredo",
    "isatap",
    "tailscale",
    "zerotier",
    "openvpn",
    "nordvpn",
    "wireguard",
    "wg",
    "tun",
    "tap",
)

# Override via COMMCLIENT_LAN_IFACE_DENYLIST="wsl,docker,vmware" etc.
def _denylist() -> tuple[str, ...]:
    env = os.environ.get("COMMCLIENT_LAN_IFACE_DENYLIST", "").strip()
    if not env:
        return _VIRTUAL_NAME_HINTS
    extra = tuple(s.strip().lower() for s in env.split(",") if s.strip())
    return _VIRTUAL_NAME_HINTS + extra


def _is_virtual_iface(iface_name: str) -> bool:
    name = iface_name.lower()
    return any(hint in name for hint in _denylist())


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LanInterface:
    """One IPv4 address on one physical or virtual adapter."""

    name: str
    address: str
    netmask: str | None = None
    is_up: bool = True
    is_virtual: bool = False
    score: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Interface enumeration
# ─────────────────────────────────────────────────────────────────────────────


def _iter_interfaces_psutil() -> Iterable[LanInterface]:  # pragma: no cover
    import psutil
    stats = psutil.net_if_stats()
    for iface_name, addrs in psutil.net_if_addrs().items():
        st = stats.get(iface_name)
        is_up = bool(st and st.isup)
        is_virtual = _is_virtual_iface(iface_name)
        for addr in addrs:
            # family 2 == AF_INET
            if int(getattr(addr.family, "value", addr.family)) != socket.AF_INET:
                continue
            ip = addr.address
            if not ip or ip.startswith("127."):
                continue
            try:
                ip_obj = ipaddress.IPv4Address(ip)
            except ValueError:
                continue
            if ip_obj.is_link_local and ip.startswith("169.254."):
                # APIPA — reachable on-link but rarely desirable for a LAN
                # server.  Skip it in the default pick-list, but still
                # expose it in full-enumeration results so the caller can
                # inspect.
                score = 1
            elif ip_obj.is_private:
                score = 10 if not is_virtual else 3
            else:
                # Public address — we're on a directly-connected WAN.
                # Still allow, but rank lower than private.
                score = 5
            yield LanInterface(
                name=iface_name,
                address=ip,
                netmask=getattr(addr, "netmask", None),
                is_up=is_up,
                is_virtual=is_virtual,
                score=score + (5 if is_up else 0),
            )


def _iter_interfaces_stdlib() -> Iterable[LanInterface]:
    """
    Fallback enumeration without psutil. Uses `socket.getaddrinfo` on the
    hostname, plus the UDP-connect trick to get at least one good IP.
    """
    seen: set[str] = set()

    def _emit(ip: str) -> LanInterface | None:
        if ip in seen or ip.startswith("127."):
            return None
        seen.add(ip)
        try:
            ip_obj = ipaddress.IPv4Address(ip)
        except ValueError:
            return None
        if ip_obj.is_private:
            score = 10
        elif ip_obj.is_link_local:
            score = 1
        else:
            score = 5
        return LanInterface(
            name="auto", address=ip, netmask=None, is_up=True,
            is_virtual=False, score=score + 5,
        )

    # Try the UDP-connect trick first (most reliable for "primary LAN IP").
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect(("203.0.113.1", 1))  # TEST-NET-3 (RFC 5737)
        primary_ip = sock.getsockname()[0]
        iface = _emit(primary_ip)
        if iface is not None:
            yield iface
    except OSError:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # Then try every hostname-resolved address.
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            iface = _emit(ip)
            if iface is not None:
                yield iface
    except socket.gaierror:
        pass


def enumerate_lan_interfaces() -> list[LanInterface]:
    """
    Return every candidate IPv4 interface, highest-priority first.
    Result is cached per-process (invalidate via `_reset_cache`).
    """
    return list(_enumerate_cached())


@lru_cache(maxsize=1)
def _enumerate_cached() -> tuple[LanInterface, ...]:
    iterator = _iter_interfaces_psutil() if _HAVE_PSUTIL else _iter_interfaces_stdlib()
    items = list(iterator)
    # Sort: higher score first, then prefer non-virtual, then iface name.
    items.sort(key=lambda i: (-i.score, i.is_virtual, i.name))
    return tuple(items)


def _reset_cache() -> None:
    """Invalidate the enumeration cache (test helper)."""
    _enumerate_cached.cache_clear()


# ─────────────────────────────────────────────────────────────────────────────
# Public convenience accessors
# ─────────────────────────────────────────────────────────────────────────────


def primary_lan_ip() -> str:
    """
    Return the single best IP to announce to clients. Falls back to
    127.0.0.1 if enumeration finds nothing.
    """
    for iface in enumerate_lan_interfaces():
        if iface.is_up and not iface.is_virtual:
            return iface.address
    for iface in enumerate_lan_interfaces():
        if iface.is_up:
            return iface.address
    return "127.0.0.1"


def all_announce_ips() -> list[str]:
    """
    Every IP worth advertising as an ICE host candidate — usable for
    multi-homed servers where mediasoup's single `announcedIp` leaves
    some clients unable to reach the SFU.
    """
    return [
        i.address for i in enumerate_lan_interfaces()
        if i.is_up and not i.is_virtual
    ]


def _subnet_of(iface: LanInterface) -> ipaddress.IPv4Network | None:
    """Best-effort IPv4 network for `iface`."""
    if not iface.netmask:
        return None
    try:
        return ipaddress.IPv4Network(
            f"{iface.address}/{iface.netmask}", strict=False,
        )
    except (ValueError, ipaddress.AddressValueError):
        return None


def lan_origins(ports: tuple[int, ...] = (3000, 5173, 8080)) -> list[str]:
    """
    Build a CORS-safe list of LAN origins: the server's own IPs on each
    of the given ports, plus the Electron `app://.` scheme and common
    localhost ports. Used by `app.core.lan_cors.attach_lan_cors`.
    """
    origins: list[str] = [
        "app://.",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]
    for ip in all_announce_ips():
        for port in ports:
            origins.append(f"http://{ip}:{port}")
            origins.append(f"https://{ip}:{port}")
    # Operator override — extra explicit origins or a "*" kill-switch.
    extra = os.environ.get("COMMCLIENT_EXTRA_CORS_ORIGINS", "").strip()
    if extra == "*":
        return ["*"]
    if extra:
        for o in extra.split(","):
            o = o.strip()
            if o and o not in origins:
                origins.append(o)
    # Deduplicate while keeping order.
    seen: set[str] = set()
    ordered: list[str] = []
    for o in origins:
        if o not in seen:
            seen.add(o)
            ordered.append(o)
    return ordered


def lan_origin_regex() -> str:
    """
    Regex that matches any `http(s)://<private-ip>:<port>` origin — useful
    when we can't enumerate exhaustively (mobile hotspots, DHCP churn).

    Matches RFC 1918 + CGNAT + link-local ranges.
    """
    # 10.0.0.0/8 | 172.16.0.0/12 | 192.168.0.0/16 | 169.254.0.0/16 | 100.64.0.0/10
    return (
        r"^https?://("
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|169\.254\.\d{1,3}\.\d{1,3}"
        r"|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}"
        r"|localhost"
        r"|127\.0\.0\.1"
        r")(:\d{1,5})?$"
    )


def enrich_ice_servers_with_all_lan_ips(
    base_entries: list[dict],
) -> list[dict]:
    """
    Given the STUN/TURN entry list built by `ice_config_service`, return a
    copy that also advertises STUN on every detected LAN IP — so multi-homed
    servers give every client a reachable host candidate.

    No-op when the current entries already include every LAN IP.
    """
    existing: set[str] = set()
    out: list[dict] = []
    for entry in base_entries:
        out.append(dict(entry))
        for url in entry.get("urls", []):
            existing.add(url)

    # Pull STUN port from env/config.
    port = int(os.environ.get("STUN_PORT", "3478") or 3478)

    extra_stun: list[str] = []
    for ip in all_announce_ips():
        url = f"stun:{ip}:{port}"
        if url not in existing:
            extra_stun.append(url)
            existing.add(url)
    if extra_stun:
        out.append({"urls": extra_stun})
    return out


__all__ = [
    "LanInterface",
    "enumerate_lan_interfaces",
    "primary_lan_ip",
    "all_announce_ips",
    "lan_origins",
    "lan_origin_regex",
    "enrich_ice_servers_with_all_lan_ips",
]
