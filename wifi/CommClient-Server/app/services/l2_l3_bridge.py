"""
L2 / L3 bridge utilities — for lab and multi-tenant deployments.

Why this exists
---------------
Helen normally sits ABOVE the network layer — it doesn't manage MAC
addresses or route IP packets, it pushes JSON over TCP/UDP. But two
deployment scenarios genuinely benefit from application-level L2/L3
bridge orchestration:

  1. **Lab / CI**: spin up four virtual servers on a single host and
     test the federation/mesh code paths end-to-end without VLANs.
     Each "server" needs its own MAC + IP. We create TUN/TAP
     interfaces and bridge them.

  2. **Multi-subnet on-prem**: an operator runs Helen-Server on a
     campus where each building has its own subnet, and they want
     Helen-Server itself to expose an L3 gateway endpoint that
     bridges packets between subnets so participants in building A
     can reach building B's mDNS-advertised peers.

This module provides:

  * ``create_tap_interface`` — create a TAP (L2) interface and
    attach it to a Linux bridge.
  * ``create_tun_interface`` — create a TUN (L3) interface and
    bind a /30 link.
  * ``add_route`` / ``remove_route`` — manage routing-table entries
    (Linux + Windows).
  * ``arp_resolve`` — application-level ARP query with caching
    (used by the existing peer-discovery path under the hood).

100% LAN
--------
Everything is local kernel/CLI orchestration. No external service.

Caveats
-------
* TAP/TUN creation needs ``CAP_NET_ADMIN`` (root or systemd
  ``AmbientCapabilities=CAP_NET_ADMIN``).
* Windows TAP requires the OpenVPN/Wintun driver — best effort, the
  module degrades to a clear "not supported" error if absent.
* Most operators **don't need this module**. It's a power-user
  helper for the two scenarios above.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


class _PrivilegeError(RuntimeError):
    pass


def _ensure_linux() -> None:
    if os.name == "nt":
        raise NotImplementedError(
            "L2/L3 bridge utilities currently target Linux. On Windows "
            "use the WireGuard manager (wireguard_manager.py) for L3 "
            "or `New-NetSwitch` PowerShell cmdlets for L2.",
        )


def _ensure_root() -> None:
    if os.name != "nt" and os.geteuid() != 0:
        raise _PrivilegeError(
            "TAP/TUN/route operations need CAP_NET_ADMIN. Run Helen "
            "via systemd with AmbientCapabilities=CAP_NET_ADMIN, or "
            "skip this module if you don't need bridge orchestration.",
        )


def _run(cmd: list[str], *, check: bool = True, timeout: float = 8.0) -> subprocess.CompletedProcess:
    """Wrapper that records the command + output for diagnostics."""
    logger.debug("l2l3_bridge_run cmd=%s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd, check=check, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "l2l3_bridge_cmd_failed cmd=%s rc=%d stderr=%s",
            " ".join(cmd), exc.returncode,
            (exc.stderr or "")[:200],
        )
        raise


# ── TAP (L2) interfaces ────────────────────────────────────────────


@dataclass
class TapInterface:
    name: str
    mac_address: str
    bridge: Optional[str] = None


def create_tap_interface(
    name: str, *,
    mac_address: Optional[str] = None,
    bridge: Optional[str] = None,
) -> TapInterface:
    """Create a TAP device. If ``bridge`` is supplied, attach the TAP
    to it (the Linux bridge must already exist — create it once with
    ``ip link add <bridge> type bridge``)."""
    _ensure_linux()
    _ensure_root()
    _run(["ip", "tuntap", "add", "dev", name, "mode", "tap"])
    if mac_address:
        _run(["ip", "link", "set", "dev", name, "address", mac_address])
    _run(["ip", "link", "set", "dev", name, "up"])
    if bridge:
        _run(["ip", "link", "set", "dev", name, "master", bridge])
    # Read back the assigned MAC if the caller didn't specify one.
    if not mac_address:
        out = _run(["ip", "-o", "link", "show", "dev", name],
                   check=False).stdout
        for tok in out.split():
            if tok.count(":") == 5 and len(tok) == 17:
                mac_address = tok
                break
    return TapInterface(name=name, mac_address=mac_address or "",
                        bridge=bridge)


def destroy_interface(name: str) -> None:
    _ensure_linux()
    _ensure_root()
    _run(["ip", "link", "delete", "dev", name], check=False)


# ── TUN (L3) interfaces ────────────────────────────────────────────


@dataclass
class TunInterface:
    name: str
    address: str
    peer: Optional[str] = None


def create_tun_interface(
    name: str, *,
    address: str,
    peer: Optional[str] = None,
    mtu: int = 1420,
) -> TunInterface:
    """Create a TUN device with a /30 (or operator-supplied prefix)
    address. If ``peer`` is supplied, the TUN is treated as a
    point-to-point link."""
    _ensure_linux()
    _ensure_root()
    _run(["ip", "tuntap", "add", "dev", name, "mode", "tun"])
    if peer:
        _run(["ip", "addr", "add", address, "peer", peer, "dev", name])
    else:
        _run(["ip", "addr", "add", address, "dev", name])
    _run(["ip", "link", "set", "dev", name, "mtu", str(mtu)])
    _run(["ip", "link", "set", "dev", name, "up"])
    return TunInterface(name=name, address=address, peer=peer)


# ── Route management ───────────────────────────────────────────────


def add_route(destination_cidr: str, *,
              gateway: Optional[str] = None,
              interface: Optional[str] = None,
              metric: int = 100) -> None:
    """Add a route. Either ``gateway`` or ``interface`` (or both)
    must be supplied. Cross-platform: Linux uses ``ip route``,
    Windows uses ``route ADD``."""
    if os.name == "nt":
        cmd = ["route", "ADD", destination_cidr.split("/")[0],
               "MASK", _cidr_mask(destination_cidr)]
        if gateway:
            cmd.append(gateway)
        cmd += ["METRIC", str(metric)]
        if interface:
            cmd += ["IF", _windows_iface_index(interface)]
        _run(cmd)
        return
    _ensure_root()
    cmd = ["ip", "route", "add", destination_cidr]
    if gateway:
        cmd += ["via", gateway]
    if interface:
        cmd += ["dev", interface]
    cmd += ["metric", str(metric)]
    _run(cmd)


def remove_route(destination_cidr: str, *,
                 gateway: Optional[str] = None,
                 interface: Optional[str] = None) -> None:
    if os.name == "nt":
        cmd = ["route", "DELETE", destination_cidr.split("/")[0]]
        if gateway:
            cmd.append(gateway)
        _run(cmd, check=False)
        return
    _ensure_root()
    cmd = ["ip", "route", "del", destination_cidr]
    if gateway:
        cmd += ["via", gateway]
    if interface:
        cmd += ["dev", interface]
    _run(cmd, check=False)


def _cidr_mask(cidr: str) -> str:
    import ipaddress
    return str(ipaddress.ip_network(cidr, strict=False).netmask)


def _windows_iface_index(name: str) -> str:
    """Best effort — use Get-NetAdapter via PowerShell. Returns the
    interface index as a string."""
    if not shutil.which("powershell"):
        return "0"
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-NetAdapter -Name '{name}').ifIndex"],
        capture_output=True, text=True, timeout=8,
    )
    return out.stdout.strip() or "0"


# ── ARP cache wrapper ──────────────────────────────────────────────


def arp_table() -> list[dict]:
    """Snapshot the OS ARP cache. Helen's external_routers.py already
    does this — exposing it here as a public helper for consistency."""
    if os.name == "nt":
        out = subprocess.run(["arp", "-a"], capture_output=True,
                             text=True, timeout=4)
        rows = []
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and "-" in parts[1]:
                rows.append({
                    "ip": parts[0],
                    "mac": parts[1].replace("-", ":"),
                    "type": parts[2] if len(parts) > 2 else "",
                })
        return rows
    out = subprocess.run(["ip", "neigh"], capture_output=True,
                         text=True, timeout=4)
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5:
            rows.append({"ip": parts[0], "mac": parts[4]})
    return rows


# ── Asyncio-friendly wrappers ──────────────────────────────────────


async def create_tap_interface_async(name: str, **kw) -> TapInterface:
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: create_tap_interface(name, **kw),
    )


async def create_tun_interface_async(name: str, **kw) -> TunInterface:
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: create_tun_interface(name, **kw),
    )


async def add_route_async(destination_cidr: str, **kw) -> None:
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: add_route(destination_cidr, **kw),
    )
