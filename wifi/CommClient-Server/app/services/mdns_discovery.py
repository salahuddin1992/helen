"""
mDNS / DNS-SD discovery — parallel channel to UDP broadcast.

UDP broadcast (port 41234-41237) is the primary discovery mechanism
on the LAN, but it has two weak spots:

  1. Some routers / managed switches drop directed broadcasts.
  2. macOS / iOS networks lean heavily on Bonjour and may filter
     unknown UDP traffic but always honour multicast DNS.

This module advertises the Helen-Server as ``_helen-server._tcp.local``
and listens for the same service so peers on a Bonjour-aware LAN can
find each other without any UDP tweaks.

It is **additive** — UDP discovery still runs in parallel and gossip
still spreads transitively, so mDNS being unavailable (no zeroconf
package, network blocks multicast, etc.) is non-fatal: the module
logs a warning and exits cleanly.

Service registration
--------------------
Service type:   ``_helen-server._tcp.local.``
Service name:   ``Helen-{server_id_short}._helen-server._tcp.local.``
Properties:     server_id, cluster_id, port, version, bridge

Discovered peers are routed straight into ``peer_registry.ingest()``
using the same shape as a UDP broadcast payload, so downstream code
(gossip, federation, relay) can't tell whether a peer was found via
UDP or mDNS.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_SERVICE_TYPE = "_helen-server._tcp.local."
_DEFAULT_PORT_FALLBACK = 3000


# ── Lazy imports for the optional zeroconf dependency ────────────


def _zeroconf_available() -> bool:
    try:
        import zeroconf  # noqa: F401
        return True
    except ImportError:
        return False


# ── Listener — picks up peer service announcements ───────────────


class _PeerListener:
    """Adapter between zeroconf's ServiceListener API and our
    peer_registry.ingest call. Silently ignores our own service so
    we don't loop back."""

    def __init__(self, my_server_id: str) -> None:
        self.my_server_id = my_server_id

    def add_service(self, zc, type_, name):  # noqa: D401 (zeroconf API)
        self._handle(zc, type_, name)

    def update_service(self, zc, type_, name):
        self._handle(zc, type_, name)

    def remove_service(self, zc, type_, name):
        # Peers age out via heartbeat TTL; we don't proactively prune
        # on mDNS withdraw because the device might just be roaming.
        return

    def _handle(self, zc, type_, name) -> None:
        try:
            info = zc.get_service_info(type_, name, timeout=2000)
            if not info:
                return
            props = {}
            for k, v in (info.properties or {}).items():
                try:
                    props[k.decode() if isinstance(k, bytes) else k] = (
                        v.decode() if isinstance(v, bytes) else v
                    )
                except Exception:
                    continue
            sid = props.get("server_id") or ""
            if not sid or sid == self.my_server_id:
                return
            host = (
                socket.inet_ntoa(info.addresses[0])
                if info.addresses
                else (info.server or "").rstrip(".")
            )
            port = int(info.port or _DEFAULT_PORT_FALLBACK)
            cluster_id = props.get("cluster_id") or "default"
            bridge = str(props.get("bridge", "false")).lower() == "true"

            payload = {
                # ingest() requires this discriminator; without it the
                # mDNS-discovered peer is silently dropped.
                "type": "commclient-server",
                "server_id": sid,
                "name": props.get("name") or sid,
                "host": host,
                "port": port,
                "cluster_id": cluster_id,
                "bridge": bridge,
                "host_aliases": [],
                "version": props.get("version") or "?",
                "source": "mdns",
            }
            try:
                from app.services.peer_registry import peer_registry
                # peer_registry.ingest is sync-friendly when no event
                # loop is around; if we're in an async context, wrap.
                try:
                    asyncio.get_running_loop()
                    asyncio.create_task(
                        peer_registry.ingest(payload, from_ip=host)
                    )
                except RuntimeError:
                    # Standalone thread without loop — defer via a small helper.
                    threading.Thread(
                        target=lambda: asyncio.run(
                            peer_registry.ingest(payload, from_ip=host)
                        ),
                        daemon=True,
                    ).start()
                logger.info(
                    "mdns_peer_discovered",
                    server_id=sid[:24], host=host, port=port,
                    cluster=cluster_id, bridge=bridge,
                )
            except Exception as e:
                logger.warning("mdns_peer_ingest_failed", error=str(e))
        except Exception as e:
            logger.debug("mdns_handle_failed", name=name, error=str(e))


# ── Module-level state ───────────────────────────────────────────


_zc = None              # zeroconf.Zeroconf instance
_browser = None         # zeroconf.ServiceBrowser instance
_service_info = None    # zeroconf.ServiceInfo (our advertisement)
_running = False


def start_mdns_discovery(
    my_server_id: str,
    port: int,
    cluster_id: str,
    version: str = "1.0.0",
    bridge: bool = False,
) -> bool:
    """Register our service and start listening for peers.

    Returns True on success, False if zeroconf is unavailable or
    initialisation fails. Either way the rest of the discovery stack
    keeps working.
    """
    global _zc, _browser, _service_info, _running

    if _running:
        return True

    if not _zeroconf_available():
        logger.info("mdns_disabled_no_zeroconf")
        return False

    try:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceInfo, InterfaceChoice

        # Collect host IPs — psutil first (works reliably on Windows
        # where socket.getaddrinfo(gethostname()) can return empty
        # results inside services / containers), then fall back to
        # the classic socket lookup, then to localhost as a last resort.
        host_ips: list[str] = []

        try:
            import psutil
            for iface_name, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family != socket.AF_INET:
                        continue
                    ip = addr.address
                    if not ip:
                        continue
                    # Skip link-local, loopback (we'll add it last only
                    # if everything else fails), and any 0.0.0.0.
                    if ip.startswith("169.254.") or ip == "0.0.0.0":
                        continue
                    if ip == "127.0.0.1":
                        continue
                    if ip not in host_ips:
                        host_ips.append(ip)
        except Exception:
            pass

        if not host_ips:
            try:
                hostname = socket.gethostname()
                for fam, _, _, _, sockaddr in socket.getaddrinfo(
                    hostname, None, socket.AF_INET
                ):
                    ip = sockaddr[0]
                    if ip and ip not in host_ips and not ip.startswith("169.254."):
                        host_ips.append(ip)
            except Exception:
                pass

        # zeroconf will refuse to register a ServiceInfo with no
        # addresses. If we genuinely could not find a routable IP we
        # advertise on loopback so at least same-host discovery works.
        if not host_ips:
            host_ips = ["127.0.0.1"]

        short_id = (my_server_id or "anon")[:8]
        instance_name = f"Helen-{short_id}.{_SERVICE_TYPE}"

        # Richer TXT records — capabilities + roles + capacity hints
        # so Bonjour-aware clients can filter without an extra HTTP probe.
        capabilities_str = ""
        roles_str = ""
        capacity_summary = ""
        try:
            from app.services.node_registry import get_registry
            self_node = next(
                (n for n in get_registry().nodes(include_dead=True)
                 if n.self_node),
                None,
            )
            if self_node is not None:
                cap = self_node.capability
                capabilities_str = (
                    f"{cap.cpu_cores}c/"
                    f"{int(cap.ram_gb)}g/"
                    f"{cap.nic_gbps:.1f}gbps"
                )
                active_roles = [
                    r for r in (
                        "signaling", "messaging", "presence", "sfu",
                        "relay", "recording", "file_transfer", "metrics",
                    )
                    if getattr(self_node.roles, r, False)
                ]
                roles_str = ",".join(active_roles)
                capacity_summary = (
                    f"sock={self_node.capacity.max_concurrent_sockets},"
                    f"rooms={self_node.capacity.max_concurrent_rooms}"
                )
        except Exception:
            pass

        properties = {
            "server_id":  my_server_id,
            "cluster_id": cluster_id,
            "version":    version,
            "bridge":     "true" if bridge else "false",
            "caps":       capabilities_str,
            "roles":      roles_str,
            "capacity":   capacity_summary,
        }

        encoded_addrs = [
            socket.inet_aton(ip)
            for ip in host_ips
            if ":" not in ip and ip != "0.0.0.0"
        ]
        if not encoded_addrs:
            encoded_addrs = [socket.inet_aton("127.0.0.1")]

        info = ServiceInfo(
            type_=_SERVICE_TYPE,
            name=instance_name,
            addresses=encoded_addrs,
            port=int(port),
            properties=properties,
            server=f"helen-{short_id}.local.",
        )

        # zeroconf.Zeroconf() spins up its own event loop thread.
        # Calling it from inside an existing asyncio loop on Windows
        # raises EventLoopBlocked. Constructing inside a short-lived
        # worker thread sidesteps the check.
        #
        # Hardened version: every failure path explicitly logs WHY
        # mDNS is not running (multi-NIC perms, timeout, fallback
        # interfaces denied, ...). Previous version returned True with
        # zeroconf silently disabled when the worker timed out — the
        # operator only found out when no peers ever appeared.
        result_box: dict = {}

        def _construct() -> None:
            try:
                # Try All-interfaces first (better discovery across
                # multi-NIC Windows hosts) and fall back if denied.
                try:
                    zc = Zeroconf(interfaces=InterfaceChoice.All)
                    result_box["interfaces"] = "all"
                except Exception as nested:
                    logger.warning("mdns_all_interfaces_denied",
                                   reason=str(nested) or
                                          nested.__class__.__name__)
                    zc = Zeroconf()
                    result_box["interfaces"] = "default"
                zc.register_service(info, allow_name_change=True)
                result_box["zc"] = zc
            except Exception as exc:  # pragma: no cover
                result_box["err"] = exc

        worker = threading.Thread(target=_construct, daemon=True)
        worker.start()
        worker.join(timeout=5.0)

        if "err" in result_box:
            err = result_box["err"]
            logger.warning(
                "mdns_init_failed",
                reason=str(err) or repr(err) or err.__class__.__name__,
                hint=("If on Windows, check that 'Bonjour Service' or "
                       "Apple's mDNSResponder is not blocking port 5353"),
            )
            raise err
        if "zc" not in result_box:
            # Worker thread never finished. This usually means
            # zeroconf got stuck inside its own multicast send/recv
            # path on a host whose firewall blocks UDP 5353. We log
            # explicitly and let the caller decide how to proceed.
            logger.warning(
                "mdns_init_timed_out",
                timeout_sec=5.0,
                hint=("Zeroconf init blocked >5s. Most likely cause: "
                       "Windows Defender Firewall is dropping UDP 5353 "
                       "outbound. Allow it for this app or set "
                       "HELEN_DISABLE_BROADCAST=1 to suppress this warning."),
            )
            raise TimeoutError("zeroconf init timed out after 5s")
        if result_box.get("interfaces") == "default":
            logger.info(
                "mdns_using_default_interface",
                detail=("Multi-NIC discovery unavailable — clients on "
                         "secondary NICs may not auto-discover this "
                         "server via mDNS"),
            )

        _zc = result_box["zc"]
        _service_info = info

        listener = _PeerListener(my_server_id=my_server_id)
        _browser = ServiceBrowser(_zc, _SERVICE_TYPE, listener)
        _running = True

        logger.info(
            "mdns_started",
            instance=instance_name, port=port,
            advertised_ips=host_ips, cluster=cluster_id,
        )
        return True
    except Exception as e:
        # str(e) is empty for some zeroconf NonUnique exceptions —
        # repr() preserves the class name so debugging isn't blind.
        err_text = str(e) or repr(e) or e.__class__.__name__
        logger.warning("mdns_start_failed", error=err_text)
        try:
            if _zc is not None:
                _zc.close()
        except Exception:
            pass
        _zc, _browser, _service_info, _running = None, None, None, False
        return False


def stop_mdns_discovery() -> None:
    global _zc, _browser, _service_info, _running
    if not _running:
        return
    try:
        if _zc and _service_info:
            _zc.unregister_service(_service_info)
        if _zc:
            _zc.close()
    except Exception as e:
        logger.warning("mdns_stop_failed", error=str(e))
    finally:
        _zc, _browser, _service_info = None, None, None
        _running = False
        logger.info("mdns_stopped")


def is_running() -> bool:
    return _running


def status() -> dict:
    return {
        "running": _running,
        "available": _zeroconf_available(),
        "service_type": _SERVICE_TYPE,
    }
