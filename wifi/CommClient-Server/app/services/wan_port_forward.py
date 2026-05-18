"""
WAN port-forward manager — make Helen reachable across NATs without
ever calling a third-party service.

Why this exists
---------------
``Helen-Router/app/upnp_portmap.py`` already knows how to *ask* an
IGD-compliant router to forward a port. But:

  1. Many routers ship with UPnP disabled (Mikrotik, pfSense, all
     enterprise Ubiquiti by default).
  2. The mapping needs to be re-asserted every few hours on routers
     that ignore ``LeaseDuration=0``.
  3. After mapping, the operator has no way to *prove* the port is
     actually open from the WAN side — without hitting a public
     "what's my IP" service, which the project policy forbids.

This module wraps ``upnp_portmap`` with:

  * **Auto-detect-and-map** with retry/backoff (the manager re-runs
    every ``refresh_interval_s`` seconds and re-maps if the router
    forgot us).
  * **Vendor-specific manual instructions** rendered as a JSON block
    the admin UI can show when UPnP fails. We use the existing
    fingerprinters in ``Helen-Router/app/vendor_adapters.py`` to pick
    the right walkthrough.
  * **Self-hosted reachability test** — instead of curl-ing
    icanhazip.com, we ask another Helen-Router peer (the federation
    list) to TCP-connect back to our advertised external IP:port.
    Fully internal.

Wire shape
----------
The manager is a single :class:`WANPortForwardManager` you start once
in the lifespan. It exposes::

    await mgr.start()
    snapshot = mgr.status()        # dict for /api/admin/wan/portmap/status
    await mgr.refresh_now()        # admin-triggered re-map
    await mgr.stop()

Configuration is env-driven (kept consistent with the rest of Helen)::

    HELEN_WAN_PORTMAP_ENABLED=1
    HELEN_WAN_EXTERNAL_PORT=3000
    HELEN_WAN_INTERNAL_PORT=3000
    HELEN_WAN_PROTOCOL=TCP
    HELEN_WAN_REFRESH_S=3600
    HELEN_WAN_PEER_PROBES=https://10.0.0.6:3000,https://10.0.0.7:3000
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Vendor-specific manual instructions ─────────────────────────────


_VENDOR_INSTRUCTIONS: dict[str, list[str]] = {
    "Mikrotik": [
        "Open Winbox or WebFig at the router's LAN IP.",
        "IP → Firewall → NAT → Add new rule.",
        "Chain: dstnat. Protocol: {proto}. Dst. Port: {ext}.",
        "Action: dst-nat. To Addresses: {internal_ip}. To Ports: {int}.",
        "Apply, then verify with 'IP → Firewall → Connections'.",
    ],
    "Ubiquiti": [
        "UniFi Network → Settings → Routing & Firewall → Port Forwarding.",
        "Create Port Forward. Name: Helen-{int}.",
        "Forward IP: {internal_ip}. Port: {ext}. Protocol: {proto}.",
        "Save and wait ~30 seconds for the rule to propagate.",
    ],
    "OpenWrt": [
        "Open LuCI at http://{router_ip}/cgi-bin/luci.",
        "Network → Firewall → Port Forwards → Add.",
        "Name: Helen. Protocol: {proto}. External port: {ext}.",
        "Internal IP: {internal_ip}. Internal port: {int}.",
        "Save & Apply.",
    ],
    "pfSense": [
        "Firewall → NAT → Port Forward → Add.",
        "Interface: WAN. Protocol: {proto}. Destination port: {ext}.",
        "Redirect target IP: {internal_ip}. Redirect port: {int}.",
        "Description: Helen. Save and Apply Changes.",
    ],
    "Cisco": [
        "configure terminal",
        "ip nat inside source static {proto_l} {internal_ip} {int} interface "
        "GigabitEthernet0/0 {ext}",
        "end",
        "write memory",
    ],
    "Generic": [
        "Open the router admin page (usually http://{router_ip}).",
        "Find Port Forwarding / NAT / Virtual Server.",
        "Add a rule: External {proto}/{ext} → {internal_ip}:{int}.",
        "Save and reboot the router if changes don't take effect.",
    ],
}


def render_manual_instructions(
    vendor: Optional[str],
    *,
    external_port: int,
    internal_port: int,
    internal_ip: str,
    protocol: str = "TCP",
    router_ip: str = "192.168.1.1",
) -> list[str]:
    """Pick the closest matching vendor walkthrough and substitute the
    deployment's actual ports/IPs."""
    key = vendor or "Generic"
    if key not in _VENDOR_INSTRUCTIONS:
        key = "Generic"
    template = _VENDOR_INSTRUCTIONS[key]
    fmt = {
        "ext": external_port,
        "int": internal_port,
        "proto": protocol.upper(),
        "proto_l": protocol.lower(),
        "internal_ip": internal_ip,
        "router_ip": router_ip,
    }
    return [line.format(**fmt) for line in template]


# ── Reachability probe ──────────────────────────────────────────────


@dataclass
class ReachabilityResult:
    """One peer-probe outcome.

    ``reachable`` is True iff the peer was able to open a TCP socket to
    our advertised ``external_ip:external_port``. ``error`` carries the
    failure reason from the peer's POV (or local connect failure)."""
    peer: str
    reachable: bool
    latency_ms: Optional[float] = None
    error: Optional[str] = None


async def probe_external_reachability(
    peer_urls: list[str],
    *,
    external_ip: str,
    external_port: int,
    protocol: str = "TCP",
    timeout_s: float = 5.0,
) -> list[ReachabilityResult]:
    """Ask each Helen peer in ``peer_urls`` to TCP-connect back to our
    ``external_ip:external_port``. Returns one result per peer.

    The peer's API contract is::

        POST /api/admin/wan/probe-back
        Body: {"target_ip": "...", "target_port": 3000, "protocol": "TCP"}
        Resp: {"reachable": true, "latency_ms": 12.4}

    A peer that doesn't speak this endpoint reports as unreachable
    rather than crashing the manager."""
    results: list[ReachabilityResult] = []

    async with httpx.AsyncClient(timeout=timeout_s, verify=False) as c:
        for url in peer_urls:
            url = url.rstrip("/")
            t0 = time.perf_counter()
            try:
                r = await c.post(
                    f"{url}/api/admin/wan/probe-back",
                    json={
                        "target_ip": external_ip,
                        "target_port": external_port,
                        "protocol": protocol,
                    },
                )
                latency = (time.perf_counter() - t0) * 1000.0
                if r.status_code == 200:
                    body = r.json()
                    results.append(ReachabilityResult(
                        peer=url,
                        reachable=bool(body.get("reachable")),
                        latency_ms=body.get("latency_ms", latency),
                        error=body.get("error"),
                    ))
                else:
                    results.append(ReachabilityResult(
                        peer=url, reachable=False,
                        error=f"HTTP {r.status_code}",
                    ))
            except Exception as exc:
                results.append(ReachabilityResult(
                    peer=url, reachable=False, error=str(exc),
                ))
    return results


def probe_back_locally(target_ip: str, target_port: int,
                        protocol: str = "TCP",
                        timeout_s: float = 3.0) -> dict:
    """Server-side handler for ``POST /api/admin/wan/probe-back``.

    Performs a TCP connect to ``target_ip:target_port`` from this host
    and returns ``{reachable, latency_ms, error}``. UDP "probe" returns
    True iff we can resolve+sendto without OSError (UDP has no
    handshake to confirm reachability).
    """
    proto = protocol.upper()
    t0 = time.perf_counter()
    try:
        if proto == "UDP":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout_s)
                s.sendto(b"\x00", (target_ip, target_port))
            return {
                "reachable": True,
                "latency_ms": (time.perf_counter() - t0) * 1000.0,
                "error": None,
            }
        # TCP
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            s.connect((target_ip, target_port))
            return {
                "reachable": True,
                "latency_ms": (time.perf_counter() - t0) * 1000.0,
                "error": None,
            }
    except Exception as exc:
        return {
            "reachable": False,
            "latency_ms": (time.perf_counter() - t0) * 1000.0,
            "error": str(exc),
        }


# ── Manager ────────────────────────────────────────────────────────


@dataclass
class WANState:
    enabled: bool = False
    external_ip: Optional[str] = None
    external_port: int = 0
    internal_ip: Optional[str] = None
    internal_port: int = 0
    protocol: str = "TCP"
    last_mapped_at: Optional[float] = None
    last_refresh_at: Optional[float] = None
    last_error: Optional[str] = None
    upnp_ok: bool = False
    vendor_hint: Optional[str] = None
    manual_instructions: list[str] = field(default_factory=list)
    last_probes: list[dict] = field(default_factory=list)


class WANPortForwardManager:
    """Periodically asserts a WAN port-forward and tracks status.

    ``upnp_url`` is the device-description URL discovered by SSDP
    (``Helen-Router/app/external_routers.py``). If ``None``, the
    manager skips UPnP and only renders manual instructions on
    request.
    """

    def __init__(
        self,
        *,
        upnp_url: Optional[str] = None,
        external_port: int = 3000,
        internal_port: int = 3000,
        internal_ip: Optional[str] = None,
        protocol: str = "TCP",
        refresh_interval_s: int = 3600,
        peer_urls: Optional[list[str]] = None,
        vendor_hint: Optional[str] = None,
        router_ip: str = "192.168.1.1",
    ) -> None:
        self.upnp_url = upnp_url
        self.external_port = external_port
        self.internal_port = internal_port
        self.internal_ip = internal_ip
        self.protocol = protocol.upper()
        self.refresh_interval_s = max(60, refresh_interval_s)
        self.peer_urls = peer_urls or []
        self.vendor_hint = vendor_hint
        self.router_ip = router_ip

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._state = WANState(
            external_port=external_port,
            internal_port=internal_port,
            protocol=self.protocol,
            vendor_hint=vendor_hint,
        )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._state.enabled = True
        if self.internal_ip is None:
            self.internal_ip = _local_lan_ip() or "127.0.0.1"
        self._state.internal_ip = self.internal_ip
        self._state.manual_instructions = render_manual_instructions(
            self.vendor_hint,
            external_port=self.external_port,
            internal_port=self.internal_port,
            internal_ip=self.internal_ip,
            protocol=self.protocol,
            router_ip=self.router_ip,
        )
        self._task = asyncio.create_task(self._loop(),
                                          name="wan-portmap-mgr")
        logger.info("wan_portmap_started",
                    external_port=self.external_port,
                    internal_port=self.internal_port,
                    upnp=bool(self.upnp_url))

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        self._state.enabled = False

    async def refresh_now(self) -> dict:
        await self._refresh_once()
        return self.status()

    def status(self) -> dict:
        return {
            "enabled": self._state.enabled,
            "external_ip": self._state.external_ip,
            "external_port": self._state.external_port,
            "internal_ip": self._state.internal_ip,
            "internal_port": self._state.internal_port,
            "protocol": self._state.protocol,
            "upnp_ok": self._state.upnp_ok,
            "last_mapped_at": self._state.last_mapped_at,
            "last_refresh_at": self._state.last_refresh_at,
            "last_error": self._state.last_error,
            "vendor_hint": self._state.vendor_hint,
            "manual_instructions": self._state.manual_instructions,
            "peer_probes": self._state.last_probes,
        }

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._refresh_once()
            except Exception as exc:
                self._state.last_error = f"refresh crashed: {exc}"
                logger.warning("wan_portmap_refresh_crashed",
                               error=str(exc))
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.refresh_interval_s,
                )
            except asyncio.TimeoutError:
                continue

    async def _refresh_once(self) -> None:
        self._state.last_refresh_at = time.time()

        if self.upnp_url:
            from Helen_Router_compat import auto_map  # noqa - shim below
            ok, ext_ip = await auto_map(
                self.upnp_url,
                helen_internal_ip=self.internal_ip,
                external_port=self.external_port,
                internal_port=self.internal_port,
            )
            self._state.upnp_ok = ok
            if ok:
                self._state.last_mapped_at = time.time()
                self._state.last_error = None
                if ext_ip:
                    self._state.external_ip = ext_ip
            else:
                self._state.last_error = "UPnP AddPortMapping refused"

        # Reachability probes (independent of UPnP outcome — they tell
        # the operator whether the port is reachable end-to-end, even
        # if they configured the rule by hand).
        if self._state.external_ip and self.peer_urls:
            probes = await probe_external_reachability(
                self.peer_urls,
                external_ip=self._state.external_ip,
                external_port=self.external_port,
                protocol=self.protocol,
            )
            self._state.last_probes = [
                {
                    "peer": p.peer,
                    "reachable": p.reachable,
                    "latency_ms": p.latency_ms,
                    "error": p.error,
                }
                for p in probes
            ]


# ── Shim so we can import upnp_portmap from Helen-Router ───────────


def _local_lan_ip() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return None


# Inline thin shim — avoids hard-coupling to Helen-Router's package
# layout (which lives in a sibling repo) by reaching into upnp_portmap
# only at refresh time. We register the module under a friendly alias
# the first time the manager runs, so we don't pay the import cost
# unless the operator actually enabled WAN port-forwarding.
import sys
import importlib.util
from pathlib import Path


def _load_helen_router_compat() -> None:
    if "Helen_Router_compat" in sys.modules:
        return
    # Walk up to the wifi/ root and find Helen-Router/app/upnp_portmap.py.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "Helen-Router" / "app" / "upnp_portmap.py"
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location(
                "Helen_Router_compat", str(candidate),
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["Helen_Router_compat"] = mod
                spec.loader.exec_module(mod)
                # Re-export under the name our manager imports.
                sys.modules["Helen_Router_compat"].auto_map = (
                    mod.auto_map_for_helen_server
                )
                return
    # Not found — register a stub that always returns (False, None) so
    # the manager doesn't crash on hosts that ship Helen-Server alone.
    class _Stub:
        @staticmethod
        async def auto_map(*_a, **_kw):
            return False, None
    sys.modules["Helen_Router_compat"] = _Stub()  # type: ignore[assignment]


_load_helen_router_compat()


# ── Singleton helpers (so admin routes can reach the manager) ──────


_manager: Optional[WANPortForwardManager] = None


def configure_wan_portmap(**kwargs) -> WANPortForwardManager:
    global _manager
    _manager = WANPortForwardManager(**kwargs)
    return _manager


def get_wan_portmap() -> Optional[WANPortForwardManager]:
    return _manager


async def shutdown_wan_portmap() -> None:
    global _manager
    if _manager is not None:
        await _manager.stop()
        _manager = None


def configure_from_env() -> Optional[WANPortForwardManager]:
    """Build a manager from ``HELEN_WAN_*`` env vars. Returns None if
    the feature isn't enabled."""
    if os.environ.get("HELEN_WAN_PORTMAP_ENABLED", "0") != "1":
        return None
    peers = [
        u.strip() for u in
        os.environ.get("HELEN_WAN_PEER_PROBES", "").split(",")
        if u.strip()
    ]
    return configure_wan_portmap(
        upnp_url=os.environ.get("HELEN_WAN_UPNP_URL") or None,
        external_port=int(os.environ.get("HELEN_WAN_EXTERNAL_PORT", "3000")),
        internal_port=int(os.environ.get("HELEN_WAN_INTERNAL_PORT", "3000")),
        protocol=os.environ.get("HELEN_WAN_PROTOCOL", "TCP"),
        refresh_interval_s=int(os.environ.get("HELEN_WAN_REFRESH_S", "3600")),
        peer_urls=peers,
        vendor_hint=os.environ.get("HELEN_WAN_VENDOR") or None,
        router_ip=os.environ.get("HELEN_WAN_ROUTER_IP", "192.168.1.1"),
    )


__all__ = [
    "WANPortForwardManager",
    "WANState",
    "ReachabilityResult",
    "render_manual_instructions",
    "probe_external_reachability",
    "probe_back_locally",
    "configure_wan_portmap",
    "get_wan_portmap",
    "shutdown_wan_portmap",
    "configure_from_env",
]
