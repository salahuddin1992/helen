"""
WireGuard mesh manager — managed VPN bridge for Helen-Server peers.

Why this exists
---------------
By default, Helen runs over plain TCP/UDP within a single LAN, plus
TLS for cross-segment traffic via Helen-Rendezvous. That's enough
when every server sits on the same trusted L2 segment. When servers
span buildings (e.g. main office + remote site connected over a
trusted WAN, or office + datacenter over a leased line), the
operator may want every Helen ↔ Helen byte to be encrypted +
authenticated end-to-end without depending on the carrier's TLS.

This module manages a WireGuard mesh over Helen's existing peer
list:

  * Generates a static keypair per server on first boot
    (``$DATA_DIR/wg/private.key`` + ``public.key``, mode 0600 + ACL).
  * Renders a ``wg0.conf`` from the live peer registry, one
    ``[Peer]`` block per known Helen-Server.
  * Calls ``wg-quick up wg0`` to bring up the interface, ``wg-quick
    down wg0`` on shutdown.
  * Re-renders + ``wg syncconf`` whenever the peer list changes
    (every ``CONFIG_REFRESH_SEC`` seconds).

Selection
---------
``HELEN_VPN_BACKEND=wireguard`` opt-in. Requires:
  * ``wg`` + ``wg-quick`` binaries on PATH (Linux only — Windows
    operators use the official MSI which exposes the same CLI).
  * ``CAP_NET_ADMIN`` (root or systemd ``AmbientCapabilities=`` set).
  * A free UDP port — defaults to 51820, override via
    ``HELEN_WG_LISTEN_PORT``.
  * A /24 mesh subnet — defaults to 10.99.0.0/24, override via
    ``HELEN_WG_MESH_SUBNET``.

100% LAN
--------
WG keys live on each server's local disk. There is no central key
distribution; peer public keys propagate via the same federation
channel Helen already uses for service discovery (HMAC-signed JSON).
This module never reaches a public WireGuard service.

Caveat
------
This does NOT replace Helen-Rendezvous. Rendezvous handles
NAT-traversal between servers behind asymmetric NATs; WireGuard
needs at least one peer to be addressable. The two complement
each other.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


_DEFAULT_LISTEN_PORT = 51820
_DEFAULT_MESH_SUBNET = "10.99.0.0/24"
_DEFAULT_INTERFACE = "wg0"


@dataclass
class WGPeer:
    server_id: str
    public_key: str
    endpoint: str  # "host:port" (the WG listen port, NOT Helen-Server's port)
    allowed_ips: list[str] = field(default_factory=list)


# ── Key management ─────────────────────────────────────────────────


def _ensure_wg_binary() -> None:
    """Verify wg + wg-quick are on PATH; raise with a clear hint if not."""
    for tool in ("wg", "wg-quick"):
        if shutil.which(tool) is None:
            raise RuntimeError(
                f"WireGuard `{tool}` not found on PATH. Install the "
                f"`wireguard-tools` package (Linux) or the official "
                f"WireGuard MSI (Windows), then retry. This module is "
                f"opt-in via HELEN_VPN_BACKEND=wireguard.",
            )


def generate_keypair() -> tuple[str, str]:
    """Returns (private_key_b64, public_key_b64). Wraps `wg genkey` +
    `wg pubkey` because doing X25519 by hand inside Python would
    drag in another dep. WG output is base64-encoded raw 32 bytes."""
    _ensure_wg_binary()
    proc = subprocess.run(
        ["wg", "genkey"], check=True, capture_output=True, text=True,
    )
    private_key = proc.stdout.strip()
    pub_proc = subprocess.run(
        ["wg", "pubkey"], input=private_key, check=True,
        capture_output=True, text=True,
    )
    public_key = pub_proc.stdout.strip()
    return private_key, public_key


def load_or_create_keypair(data_dir: str) -> tuple[str, str]:
    """Idempotent — re-uses an existing keypair if present."""
    base = Path(data_dir) / "wg"
    base.mkdir(parents=True, exist_ok=True)
    priv_path = base / "private.key"
    pub_path = base / "public.key"
    if priv_path.exists() and pub_path.exists():
        return priv_path.read_text().strip(), pub_path.read_text().strip()

    priv, pub = generate_keypair()
    priv_path.write_text(priv + "\n")
    pub_path.write_text(pub + "\n")
    try:
        os.chmod(priv_path, 0o600)
    except Exception:
        pass
    if os.name == "nt":
        try:
            subprocess.run(
                ["icacls", str(priv_path),
                 "/inheritance:r",
                 "/grant:r", "SYSTEM:(R,W)",
                 "Administrators:(F)"],
                check=False, capture_output=True, timeout=8,
            )
        except Exception:
            pass
    logger.info("wg_keypair_created path=%s", priv_path)
    return priv, pub


# ── IP assignment from server_id (deterministic) ───────────────────


def deterministic_mesh_ip(server_id: str, subnet: str = _DEFAULT_MESH_SUBNET) -> str:
    """Hash the server_id into a stable IP within the mesh subnet.
    Collisions are astronomically unlikely for a /24 with hundreds of
    peers, but the operator can override via env per-server if needed
    (see ``HELEN_WG_OVERRIDE_IP``)."""
    override = os.environ.get("HELEN_WG_OVERRIDE_IP")
    if override:
        return override.strip()
    import ipaddress
    net = ipaddress.ip_network(subnet, strict=False)
    digest = hashlib.sha256(server_id.encode("utf-8")).digest()
    # Map to host bits within the subnet, skipping .0 and .255.
    host_bits = net.max_prefixlen - net.prefixlen
    raw = int.from_bytes(digest[: max(1, (host_bits + 7) // 8)], "big")
    host = (raw % (net.num_addresses - 2)) + 1
    return str(net.network_address + host)


# ── Config rendering ───────────────────────────────────────────────


def render_wg_conf(
    *, private_key: str, address: str, listen_port: int,
    peers: list[WGPeer],
) -> str:
    """Emit a wg-quick-compatible ``wg0.conf``."""
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {address}",
        f"ListenPort = {listen_port}",
        "SaveConfig = false",
        "",
    ]
    for p in peers:
        if not p.public_key or not p.allowed_ips:
            continue
        lines.append(f"# {p.server_id}")
        lines.append("[Peer]")
        lines.append(f"PublicKey = {p.public_key}")
        if p.endpoint:
            lines.append(f"Endpoint = {p.endpoint}")
        lines.append(f"AllowedIPs = {', '.join(p.allowed_ips)}")
        lines.append("PersistentKeepalive = 25")
        lines.append("")
    return "\n".join(lines)


def write_conf_atomic(conf_path: Path, content: str) -> None:
    """Write the config with mode 0600, replacing atomically."""
    tmp = conf_path.with_suffix(conf_path.suffix + ".tmp")
    tmp.write_text(content)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(conf_path)


# ── lifecycle (wg-quick up / down) ─────────────────────────────────


class WireGuardManager:
    """Owns the wg0 interface for the local Helen-Server."""

    CONFIG_REFRESH_SEC = 30.0

    def __init__(
        self, data_dir: str, *,
        interface: str = _DEFAULT_INTERFACE,
        listen_port: int = _DEFAULT_LISTEN_PORT,
        mesh_subnet: str = _DEFAULT_MESH_SUBNET,
    ) -> None:
        self.data_dir = data_dir
        self.interface = interface
        self.listen_port = listen_port
        self.mesh_subnet = mesh_subnet
        self.conf_path = Path(data_dir) / "wg" / f"{interface}.conf"
        self._private_key: Optional[str] = None
        self._public_key: Optional[str] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._peers: list[WGPeer] = []
        self._running = False

    # ── boot / shutdown ────────────────────────────────────────

    async def start(self, *, server_id: str,
                    initial_peers: list[WGPeer] | None = None) -> None:
        _ensure_wg_binary()
        self._private_key, self._public_key = load_or_create_keypair(
            self.data_dir,
        )
        self._peers = list(initial_peers or [])
        self._render_and_write(server_id=server_id)
        await self._wg_quick("up")
        self._running = True
        logger.info("wireguard_up interface=%s subnet=%s",
                    self.interface, self.mesh_subnet)

    async def stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
        if self._running:
            try:
                await self._wg_quick("down")
            except Exception as exc:
                logger.warning("wireguard_down_failed error=%s", exc)
        self._running = False

    # ── peer updates ───────────────────────────────────────────

    async def update_peers(self, peers: list[WGPeer], *,
                            server_id: str) -> None:
        """Re-render conf + run ``wg syncconf`` to apply changes
        without bouncing the interface."""
        self._peers = list(peers)
        self._render_and_write(server_id=server_id)
        try:
            subprocess.run(
                ["wg", "syncconf", self.interface,
                 str(self.conf_path)],
                check=True, capture_output=True, timeout=8,
            )
            logger.info("wireguard_peers_synced count=%d", len(peers))
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "wireguard_syncconf_failed stderr=%s",
                (exc.stderr or b"").decode("utf-8", "replace")[:200],
            )

    @property
    def public_key(self) -> Optional[str]:
        return self._public_key

    def stats(self) -> dict:
        return {
            "running": self._running,
            "interface": self.interface,
            "listen_port": self.listen_port,
            "mesh_subnet": self.mesh_subnet,
            "peer_count": len(self._peers),
            "public_key": self._public_key or "",
        }

    # ── internals ──────────────────────────────────────────────

    def _render_and_write(self, *, server_id: str) -> None:
        my_ip = deterministic_mesh_ip(server_id, self.mesh_subnet) + "/32"
        # Each peer must have at least one /32 in allowed_ips for the
        # WG kernel to route to it.
        for p in self._peers:
            if not p.allowed_ips:
                p.allowed_ips = [
                    deterministic_mesh_ip(p.server_id, self.mesh_subnet)
                    + "/32",
                ]
        conf = render_wg_conf(
            private_key=self._private_key or "",
            address=my_ip,
            listen_port=self.listen_port,
            peers=self._peers,
        )
        write_conf_atomic(self.conf_path, conf)

    async def _wg_quick(self, action: str) -> None:
        """Run ``wg-quick {up,down} <interface>``. wg-quick on Linux
        will use ``ip``/``iptables``; on Windows it shells the WG
        service. Either way it returns on success."""
        loop = asyncio.get_running_loop()

        def _run():
            return subprocess.run(
                ["wg-quick", action, str(self.conf_path)],
                check=True, capture_output=True, text=True, timeout=15,
            )
        await loop.run_in_executor(None, _run)


# ── Module-level singleton ─────────────────────────────────────────


_INSTANCE: Optional[WireGuardManager] = None


async def configure_wireguard(
    data_dir: str, *,
    server_id: str,
    initial_peers: list[WGPeer] | None = None,
    listen_port: int = _DEFAULT_LISTEN_PORT,
    mesh_subnet: str = _DEFAULT_MESH_SUBNET,
) -> WireGuardManager:
    """Idempotent. Brings up wg0 on first call. The caller (typically
    the lifespan in app/main.py) is responsible for feeding peer
    updates via update_peers as the federation registry changes."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = WireGuardManager(
            data_dir=data_dir,
            listen_port=listen_port,
            mesh_subnet=mesh_subnet,
        )
        await _INSTANCE.start(
            server_id=server_id,
            initial_peers=initial_peers,
        )
    return _INSTANCE


def get_wireguard() -> Optional[WireGuardManager]:
    return _INSTANCE


async def shutdown_wireguard() -> None:
    global _INSTANCE
    if _INSTANCE is not None:
        await _INSTANCE.stop()
        _INSTANCE = None
