"""
External-router discovery + integration.

Helen-Router speaks to **physical** network gear so it can:

  * Find every router/AP/gateway sitting on the LAN (the user's
    Mikrotik / Ubiquiti / TP-Link / OpenWrt / etc.) without manual
    config.
  * Read their status — link speed, uptime, neighbouring devices —
    via vendor-agnostic protocols (SSDP, mDNS, SNMP, ICMP, ARP).
  * Ask them to open a port for Helen-Server when running across a
    NAT'd subnet (UPnP / IGD).
  * Drive vendor-specific APIs (RouterOS REST, UniFi REST, OpenWrt
    LuCI/UBUS) when credentials are supplied.

Discovery sources (run in parallel)
-----------------------------------
  1. SSDP    — UDP 1900 multicast, looks for IGD InternetGatewayDevice
  2. mDNS    — _services._dns-sd._udp browse, finds *.local devices
  3. ARP     — ip neighbor / arp -a, scans the local subnet
  4. ICMP    — ping the default gateway + a sweep of /24
  5. SNMP    — public/private community sysDescr / sysName / ifTable

Each detected router is stored in a ``LanDevice`` record. The Helen
mesh (app/mesh.py) can use these as out-of-band peers — e.g. tunnel a
helen connection over the user's existing VPN gateway.

This module is import-safe: a missing optional dep (pysnmp, getmac…)
just disables that probe; nothing raises at import time.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional


# ── Data model ──────────────────────────────────────────────────────


@dataclass
class LanDevice:
    """A router / AP / gateway / managed switch we discovered."""
    ip: str
    mac: Optional[str] = None
    hostname: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    discovered_via: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    is_gateway: bool = False
    upnp_url: Optional[str] = None        # full IGD device description URL
    snmp_sysdescr: Optional[str] = None
    last_seen: float = field(default_factory=time.time)

    def merge(self, other: "LanDevice") -> None:
        """Fold information from a second discovery into this record."""
        if other.mac and not self.mac:
            self.mac = other.mac
        if other.hostname and not self.hostname:
            self.hostname = other.hostname
        if other.vendor and not self.vendor:
            self.vendor = other.vendor
        if other.model and not self.model:
            self.model = other.model
        if other.upnp_url:
            self.upnp_url = other.upnp_url
        if other.snmp_sysdescr:
            self.snmp_sysdescr = other.snmp_sysdescr
        for src in other.discovered_via:
            if src not in self.discovered_via:
                self.discovered_via.append(src)
        for cap in other.capabilities:
            if cap not in self.capabilities:
                self.capabilities.append(cap)
        self.is_gateway = self.is_gateway or other.is_gateway
        self.last_seen = max(self.last_seen, other.last_seen)


# ── Default-gateway lookup ──────────────────────────────────────────


def find_default_gateway() -> Optional[str]:
    """Return the IPv4 default gateway of this host, or None.

    Cross-platform: parses ``ip route`` on Linux/Mac, ``route print``
    on Windows. Falls back to None if neither is available.
    """
    try:
        if os.name == "nt":
            out = subprocess.check_output(
                ["route", "print", "-4"],
                stderr=subprocess.DEVNULL, text=True, timeout=3,
            )
            for line in out.splitlines():
                # "          0.0.0.0          0.0.0.0      192.168.1.1 ..."
                tokens = line.split()
                if len(tokens) >= 3 and tokens[0] == "0.0.0.0":
                    return tokens[2]
        else:
            try:
                out = subprocess.check_output(
                    ["ip", "route"], stderr=subprocess.DEVNULL,
                    text=True, timeout=3,
                )
            except FileNotFoundError:
                out = subprocess.check_output(
                    ["netstat", "-rn"], stderr=subprocess.DEVNULL,
                    text=True, timeout=3,
                )
            for line in out.splitlines():
                if line.startswith("default") or line.startswith("0.0.0.0"):
                    parts = line.split()
                    for tok in parts:
                        try:
                            ipaddress.ip_address(tok)
                            return tok
                        except ValueError:
                            continue
    except Exception:
        pass
    return None


def local_subnets() -> list[ipaddress.IPv4Network]:
    """Best-effort list of /24 networks the host is attached to."""
    out: list[ipaddress.IPv4Network] = []
    try:
        import psutil
        for ifname, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family != socket.AF_INET:
                    continue
                ip = a.address
                if (not ip or ip == "127.0.0.1"
                        or ip.startswith("169.254.")):
                    continue
                # Force /24 — finer mask creates too many ARP probes
                # when the OS reports a /16 LAN.
                base = ".".join(ip.split(".")[:3]) + ".0/24"
                try:
                    net = ipaddress.ip_network(base, strict=False)
                    if net not in out:
                        out.append(net)
                except ValueError:
                    pass
    except Exception:
        pass
    return out


# ── SSDP / UPnP ─────────────────────────────────────────────────────


SSDP_DISCOVER = (
    b"M-SEARCH * HTTP/1.1\r\n"
    b"HOST: 239.255.255.250:1900\r\n"
    b"MAN: \"ssdp:discover\"\r\n"
    b"MX: 2\r\n"
    b"ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    b"\r\n"
)


async def discover_ssdp(timeout_sec: float = 3.0) -> list[LanDevice]:
    """Multicast SSDP M-SEARCH for IGD devices on UDP 1900.

    Every reply is parsed for the LOCATION header which points at the
    UPnP device-description XML. We don't fetch the XML here (that's
    the upnp-port-mapping module's job); discovery just records the
    URL.
    """
    loop = asyncio.get_running_loop()
    found: dict[str, LanDevice] = {}

    class _Proto(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport
            transport.sendto(SSDP_DISCOVER, ("239.255.255.250", 1900))

        def datagram_received(self, data, addr):
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                return
            ip = addr[0]
            location = None
            server = None
            for line in text.splitlines():
                low = line.lower()
                if low.startswith("location:"):
                    location = line.split(":", 1)[1].strip()
                elif low.startswith("server:"):
                    server = line.split(":", 1)[1].strip()
            if not location:
                return
            d = LanDevice(
                ip=ip,
                upnp_url=location,
                discovered_via=["ssdp"],
                capabilities=["upnp_igd"],
                vendor=server,
            )
            existing = found.get(ip)
            if existing:
                existing.merge(d)
            else:
                found[ip] = d

    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Proto(),
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
        )
    except Exception:
        return []
    try:
        await asyncio.sleep(timeout_sec)
    finally:
        transport.close()
    return list(found.values())


# ── mDNS / Bonjour ──────────────────────────────────────────────────


_MDNS_SERVICE_TYPES = [
    # Common service types advertised by routers / APs
    "_workstation._tcp.local.",
    "_smb._tcp.local.",
    "_airport._tcp.local.",   # Apple Airport / Time Capsule
    "_ssh._tcp.local.",
    "_http._tcp.local.",
    "_printer._tcp.local.",
    "_googlecast._tcp.local.",
    "_homekit._tcp.local.",
    # OpenWrt / DD-WRT broadcast
    "_router._tcp.local.",
]


async def discover_mdns(timeout_sec: float = 3.0) -> list[LanDevice]:
    """Browse a handful of common mDNS service types and record the
    addresses that respond. Misses devices that don't advertise any
    Bonjour service — fine, the ARP/SSDP probes pick those up."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf, ServiceListener
    except ImportError:
        return []

    found: dict[str, LanDevice] = {}

    class _Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            try:
                info = zc.get_service_info(type_, name, timeout=1500)
                if not info or not info.addresses:
                    return
                ip = socket.inet_ntoa(info.addresses[0])
                hostname = (info.server or "").rstrip(".") or None
                vendor = None
                # Some Bonjour TXT records expose vendor strings
                for k, v in (info.properties or {}).items():
                    try:
                        kk = k.decode() if isinstance(k, bytes) else k
                        vv = v.decode() if isinstance(v, bytes) else v
                    except Exception:
                        continue
                    if kk and kk.lower() in ("model", "md", "manufacturer"):
                        vendor = vv
                d = LanDevice(
                    ip=ip,
                    hostname=hostname,
                    vendor=vendor,
                    discovered_via=["mdns"],
                    capabilities=[type_.split(".")[0].lstrip("_")],
                )
                if ip in found:
                    found[ip].merge(d)
                else:
                    found[ip] = d
            except Exception:
                pass

        def update_service(self, *_a):
            pass

        def remove_service(self, *_a):
            pass

    zc = Zeroconf()
    browsers: list = []
    try:
        listener = _Listener()
        # Build the browser list inside the try so a failing
        # constructor doesn't leak the Zeroconf instance.
        browsers = [
            ServiceBrowser(zc, t, listener) for t in _MDNS_SERVICE_TYPES
        ]
        await asyncio.sleep(timeout_sec)
    finally:
        # Cancel browsers explicitly so their internal threads exit
        # before we close Zeroconf — otherwise close() races with
        # in-flight resolve callbacks on slow networks.
        for sb in browsers:
            try:
                sb.cancel()
            except Exception:
                pass
        try:
            zc.close()
        except Exception:
            pass
    return list(found.values())


# ── ARP scan + ping sweep ───────────────────────────────────────────


async def discover_arp() -> list[LanDevice]:
    """Read the OS ARP table — every IP we've already exchanged a
    packet with is in there. Cheap, quiet, no scanning."""
    devices: dict[str, LanDevice] = {}

    cmd = ["arp", "-a"] if os.name == "nt" else ["ip", "neigh"]
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL,
            text=True, timeout=3,
        )
    except Exception:
        return []

    for line in out.splitlines():
        # Windows arp -a:  "  10.0.0.1            aa-bb-cc-dd-ee-ff     dynamic"
        # Linux ip neigh:  "10.0.0.1 dev wlan0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
        toks = line.split()
        if len(toks) < 2:
            continue
        ip_str, mac = None, None
        for tok in toks:
            try:
                ipaddress.ip_address(tok)
                ip_str = tok
                break
            except ValueError:
                continue
        for tok in toks:
            t = tok.replace("-", ":").lower()
            if (len(t) == 17 and t.count(":") == 5
                    and all(c in "0123456789abcdef:" for c in t)):
                mac = t
                break
        if not ip_str or ip_str.startswith("224.") or ip_str == "0.0.0.0":
            continue
        d = LanDevice(
            ip=ip_str, mac=mac,
            discovered_via=["arp"],
        )
        devices[ip_str] = d
    return list(devices.values())


async def ping_sweep(subnet: ipaddress.IPv4Network,
                      concurrency: int = 32,
                      timeout_ms: int = 200) -> list[str]:
    """Async ping every host in ``subnet``. Returns list of live IPs.

    Limited to /24 (254 probes) to keep the noise reasonable.
    """
    sem = asyncio.Semaphore(concurrency)
    alive: list[str] = []

    async def probe(ip: str):
        async with sem:
            cmd = (
                ["ping", "-n", "1", "-w", str(timeout_ms), ip]
                if os.name == "nt"
                else ["ping", "-c", "1", "-W", str(timeout_ms / 1000), ip]
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await proc.wait()
                if rc == 0:
                    alive.append(ip)
            except Exception:
                pass

    hosts = list(subnet.hosts())
    if len(hosts) > 254:
        hosts = hosts[:254]
    await asyncio.gather(*(probe(str(h)) for h in hosts))
    return alive


# ── Top-level entry: full discovery sweep ───────────────────────────


async def discover_all(
    *,
    do_ping_sweep: bool = False,
    ssdp_timeout: float = 3.0,
    mdns_timeout: float = 3.0,
) -> list[LanDevice]:
    """Run every probe in parallel, merge results by IP, mark the
    default gateway. ``do_ping_sweep=True`` walks every /24 the host
    is attached to — slow, off by default."""
    gw = find_default_gateway()

    tasks = [
        asyncio.create_task(discover_ssdp(ssdp_timeout)),
        asyncio.create_task(discover_mdns(mdns_timeout)),
        asyncio.create_task(discover_arp()),
    ]
    if do_ping_sweep:
        async def sweep_all() -> list[LanDevice]:
            out: list[LanDevice] = []
            for net in local_subnets():
                ips = await ping_sweep(net)
                out.extend(LanDevice(ip=ip, discovered_via=["ping"])
                           for ip in ips)
            return out
        tasks.append(asyncio.create_task(sweep_all()))

    merged: dict[str, LanDevice] = {}
    for fut in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(fut, Exception):
            continue
        for d in fut:
            if d.ip in merged:
                merged[d.ip].merge(d)
            else:
                merged[d.ip] = d

    if gw and gw in merged:
        merged[gw].is_gateway = True
        if "default_gateway" not in merged[gw].capabilities:
            merged[gw].capabilities.append("default_gateway")
    elif gw:
        merged[gw] = LanDevice(
            ip=gw, is_gateway=True,
            discovered_via=["route_table"],
            capabilities=["default_gateway"],
        )

    return list(merged.values())
