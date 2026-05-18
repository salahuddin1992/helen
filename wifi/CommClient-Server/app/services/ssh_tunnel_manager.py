"""
SSH tunnel manager — paramiko-based local + reverse port forwarding
between Helen-Servers.

Why this exists
---------------
WireGuard (added in v9) gives the cleanest peer-to-peer encrypted
overlay, but it needs a UDP-friendly path between hosts. Some
deployments sit behind firewalls that only allow outbound TCP/22 —
typical "jump host" environments. SSH tunnels work there:

  * **Local forward**: ``ssh -L 0.0.0.0:13000:peer-host:3000`` —
    expose a remote Helen-Server's port on the local interface.
  * **Reverse forward**: ``ssh -R 0.0.0.0:13000:localhost:3000`` —
    let a peer reach our Helen-Server through their SSH session
    (useful when WE are behind NAT and they're not).

This module wraps paramiko's ``Transport`` + ``request_port_forward``
so Helen can spawn either type of tunnel programmatically. Tunnels
are tracked in a registry so admin can see them via
``/api/admin/transports/ssh/status``.

Selection
---------
Opt-in via ``HELEN_SSH_TUNNELS_ENABLED=1``. Tunnel definitions come
from ``HELEN_SSH_TUNNELS`` env var as a CSV of specs:

  HELEN_SSH_TUNNELS=local:user@10.0.0.5:22:13000:peer-host:3000,reverse:user@10.0.0.5:22:13443:localhost:3443

Each spec: ``<direction>:<user>@<host>:<port>:<bind_port>:<dest_host>:<dest_port>``

100% LAN
--------
The SSH server must be a LAN host. paramiko verifies host keys
against ``data/ssh-known-hosts`` (auto-populated on first connect
with TOFU semantics — operator can pre-seed for stricter checks).

Auth
----
Key-based only — never passwords. Helen reads
``data/ssh-client.key`` (private). Generate it once with
``ssh-keygen -t ed25519 -f data/ssh-client.key``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


class SSHNotInstalledError(RuntimeError):
    pass


@dataclass
class TunnelSpec:
    direction: str           # "local" or "reverse"
    user: str
    host: str
    port: int = 22
    bind_port: int = 0       # local listen port (or remote-bind for reverse)
    dest_host: str = "localhost"
    dest_port: int = 0


@dataclass
class TunnelState:
    spec: TunnelSpec
    status: str = "starting"   # starting | up | down | error
    error: Optional[str] = None
    bytes_in: int = 0
    bytes_out: int = 0


def parse_tunnel_specs(csv: str) -> list[TunnelSpec]:
    """Parse the env CSV. Malformed entries are skipped with a warning."""
    out: list[TunnelSpec] = []
    if not csv:
        return out
    for raw in csv.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            direction, rest = raw.split(":", 1)
            user_at, port_s, bind_s, dest_host, dest_port_s = rest.split(":", 4)
            user, host = user_at.split("@", 1)
            out.append(TunnelSpec(
                direction=direction,
                user=user, host=host,
                port=int(port_s),
                bind_port=int(bind_s),
                dest_host=dest_host,
                dest_port=int(dest_port_s),
            ))
        except (ValueError, IndexError) as exc:
            logger.warning("ssh_tunnel_spec_malformed raw=%s error=%s",
                           raw, exc)
    return out


# ── Tunnel manager ─────────────────────────────────────────────────


class SSHTunnelManager:
    """Owns the paramiko transports + tunnel threads. Each tunnel
    runs in its own daemon thread because paramiko's Channel API is
    synchronous; we cancel them on shutdown by closing the underlying
    transport."""

    def __init__(self, *, key_path: Optional[str] = None,
                 known_hosts_path: Optional[str] = None) -> None:
        self.key_path = key_path
        self.known_hosts_path = known_hosts_path
        self._tunnels: dict[str, TunnelState] = {}
        self._transports: dict[str, object] = {}  # paramiko.Transport
        self._threads: list[threading.Thread] = []
        self._shutdown = threading.Event()

    def _ensure_paramiko(self):
        try:
            import paramiko  # type: ignore
            return paramiko
        except ImportError as exc:
            raise SSHNotInstalledError(
                "`paramiko` is not installed. Add `paramiko>=3.4` to "
                "requirements.txt and rebuild Helen-Server, OR keep "
                "the default WebSocket reverse-tunnel + WireGuard "
                "stack.",
            ) from exc

    def _key_id(self, spec: TunnelSpec) -> str:
        return f"{spec.direction}:{spec.user}@{spec.host}:{spec.port}->" \
               f"{spec.dest_host}:{spec.dest_port}"

    async def start_all(self, specs: list[TunnelSpec]) -> None:
        for spec in specs:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda s=spec: self._start_one(s),
            )

    def _start_one(self, spec: TunnelSpec) -> None:
        paramiko = self._ensure_paramiko()
        key_id = self._key_id(spec)
        st = TunnelState(spec=spec)
        self._tunnels[key_id] = st
        try:
            sock_to_remote = paramiko.SSHClient()
            sock_to_remote.set_missing_host_key_policy(
                paramiko.AutoAddPolicy(),
            )
            if self.known_hosts_path and \
                    os.path.exists(self.known_hosts_path):
                sock_to_remote.load_host_keys(self.known_hosts_path)

            pkey = None
            if self.key_path and os.path.exists(self.key_path):
                # Try every algorithm paramiko supports; first match wins.
                for cls_name in ("Ed25519Key", "RSAKey", "ECDSAKey"):
                    try:
                        cls = getattr(paramiko, cls_name)
                        pkey = cls.from_private_key_file(self.key_path)
                        break
                    except Exception:
                        continue

            sock_to_remote.connect(
                hostname=spec.host, port=spec.port,
                username=spec.user, pkey=pkey,
                allow_agent=False, look_for_keys=False,
                timeout=8, banner_timeout=8,
            )
            transport = sock_to_remote.get_transport()
            self._transports[key_id] = transport
            assert transport is not None

            if spec.direction == "local":
                # Local-forward: open a listening socket on
                # 0.0.0.0:bind_port that pipes through SSH to
                # dest_host:dest_port.
                t = threading.Thread(
                    target=self._serve_local_forward,
                    args=(spec, transport, st),
                    daemon=True, name=f"ssh-fwd-local-{key_id[:32]}",
                )
                t.start()
                self._threads.append(t)
            elif spec.direction == "reverse":
                # Reverse-forward: SSH server starts listening on its
                # interface, paramiko callback handles incoming streams.
                transport.request_port_forward(
                    "0.0.0.0", spec.bind_port,
                    handler=lambda chan, origin, server, _spec=spec, _st=st:
                        self._reverse_handler(chan, _spec, _st),
                )
            else:
                raise ValueError(f"unknown direction: {spec.direction}")

            st.status = "up"
            logger.info("ssh_tunnel_up direction=%s peer=%s:%d local=%d",
                        spec.direction, spec.host, spec.port, spec.bind_port)
        except Exception as exc:
            st.status = "error"
            st.error = str(exc)[:200]
            logger.warning("ssh_tunnel_failed key=%s error=%s",
                           key_id, exc)

    def _serve_local_forward(self, spec: TunnelSpec, transport,
                              st: TunnelState) -> None:
        import select
        import socket as _socket
        try:
            srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", spec.bind_port))
            srv.listen(8)
            while not self._shutdown.is_set():
                rlist, _, _ = select.select([srv], [], [], 1.0)
                if not rlist:
                    continue
                client, _addr = srv.accept()
                t = threading.Thread(
                    target=self._pipe_local_to_ssh,
                    args=(client, spec, transport, st),
                    daemon=True,
                )
                t.start()
        except Exception as exc:
            st.status = "error"
            st.error = str(exc)[:200]

    def _pipe_local_to_ssh(self, client, spec: TunnelSpec, transport,
                            st: TunnelState) -> None:
        try:
            chan = transport.open_channel(
                "direct-tcpip",
                (spec.dest_host, spec.dest_port),
                client.getpeername(),
            )
            self._splice(client, chan, st)
        except Exception as exc:
            st.error = str(exc)[:200]
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _reverse_handler(self, chan, spec: TunnelSpec,
                          st: TunnelState) -> None:
        import socket as _socket
        try:
            target = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            target.connect((spec.dest_host, spec.dest_port))
            self._splice(chan, target, st)
        except Exception as exc:
            st.error = str(exc)[:200]
            try:
                chan.close()
            except Exception:
                pass

    @staticmethod
    def _splice(a, b, st: TunnelState) -> None:
        """Bidirectional byte pump. Tracks counters for stats."""
        import select
        try:
            while True:
                rlist, _, _ = select.select([a, b], [], [], 5.0)
                if a in rlist:
                    data = a.recv(4096)
                    if not data:
                        break
                    b.send(data)
                    st.bytes_out += len(data)
                if b in rlist:
                    data = b.recv(4096)
                    if not data:
                        break
                    a.send(data)
                    st.bytes_in += len(data)
        except Exception:
            pass
        finally:
            for sock in (a, b):
                try:
                    sock.close()
                except Exception:
                    pass

    async def stop_all(self) -> None:
        self._shutdown.set()
        for tr in list(self._transports.values()):
            try:
                tr.close()
            except Exception:
                pass
        self._tunnels.clear()
        self._transports.clear()

    def stats(self) -> dict:
        return {
            "tunnel_count": len(self._tunnels),
            "tunnels": [
                {
                    "id": tid,
                    "direction": st.spec.direction,
                    "peer": f"{st.spec.host}:{st.spec.port}",
                    "bind_port": st.spec.bind_port,
                    "dest": f"{st.spec.dest_host}:{st.spec.dest_port}",
                    "status": st.status,
                    "error": st.error,
                    "bytes_in": st.bytes_in,
                    "bytes_out": st.bytes_out,
                }
                for tid, st in self._tunnels.items()
            ],
        }


# ── Module-level singleton ─────────────────────────────────────────


_INSTANCE: Optional[SSHTunnelManager] = None


async def configure_ssh_tunnels(
    specs: list[TunnelSpec], *,
    key_path: Optional[str] = None,
    known_hosts_path: Optional[str] = None,
) -> SSHTunnelManager:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = SSHTunnelManager(
            key_path=key_path, known_hosts_path=known_hosts_path,
        )
        await _INSTANCE.start_all(specs)
    return _INSTANCE


def get_ssh_tunnels() -> Optional[SSHTunnelManager]:
    return _INSTANCE


async def shutdown_ssh_tunnels() -> None:
    global _INSTANCE
    if _INSTANCE is not None:
        await _INSTANCE.stop_all()
        _INSTANCE = None
