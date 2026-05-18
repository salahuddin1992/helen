"""
SystemInspector — gathers host/CPU/RAM/disk + network interface inventory
and runs reachability probes for the operator onboarding wizard.

Uses ``psutil`` for system stats and ``socket`` for primary-IP detection.
Probes are best-effort and never raise; failures are returned as
``{"ok": False, "error": "..."}`` per-interface.
"""
from __future__ import annotations

import asyncio
import platform
import socket
import time
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except Exception:  # pragma: no cover
    _HAS_PSUTIL = False


class SystemInspector:
    """Async wrapper over blocking psutil/socket calls."""

    # ── high-level ───────────────────────────────────────
    async def info(self) -> dict[str, Any]:
        return await asyncio.get_event_loop().run_in_executor(None, self._info_sync)

    async def network_probe(
        self, interfaces: list[str], subnets: list[str],
    ) -> dict[str, Any]:
        return await asyncio.get_event_loop().run_in_executor(
            None, self._network_probe_sync, interfaces, subnets,
        )

    # ── sync implementations ─────────────────────────────
    def _info_sync(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "hostname": socket.gethostname(),
            "fqdn": socket.getfqdn(),
            "os": platform.platform(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "primary_ip": self._primary_ip(),
            "interfaces": self._interfaces(),
            "cpu": self._cpu(),
            "ram": self._ram(),
            "disks": self._disks(),
            "uptime_seconds": self._uptime(),
        }
        return out

    def _primary_ip(self) -> str | None:
        try:
            # Connectionless trick to find the outbound interface IP.
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.settimeout(0.5)
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return None

    def _interfaces(self) -> list[dict[str, Any]]:
        interfaces: list[dict[str, Any]] = []
        if not _HAS_PSUTIL:
            return interfaces
        try:
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            for name, addr_list in addrs.items():
                ipv4 = next((a.address for a in addr_list
                            if getattr(a, "family", None) == socket.AF_INET), None)
                netmask = next((a.netmask for a in addr_list
                               if getattr(a, "family", None) == socket.AF_INET), None)
                broadcast = next((a.broadcast for a in addr_list
                                 if getattr(a, "family", None) == socket.AF_INET), None)
                ipv6 = next((a.address for a in addr_list
                            if getattr(a, "family", None) == socket.AF_INET6), None)
                mac = next((a.address for a in addr_list
                           if getattr(a, "family", None) == getattr(psutil, "AF_LINK", -1)), None)
                stat = stats.get(name)
                interfaces.append({
                    "name": name,
                    "ipv4": ipv4,
                    "netmask": netmask,
                    "broadcast": broadcast,
                    "ipv6": ipv6,
                    "mac": mac,
                    "is_up": bool(stat.isup) if stat else None,
                    "speed_mbps": int(stat.speed) if stat else None,
                    "mtu": int(stat.mtu) if stat else None,
                })
        except Exception as e:
            logger.warning("interface_enumeration_failed", error=str(e))
        return interfaces

    def _cpu(self) -> dict[str, Any]:
        if not _HAS_PSUTIL:
            return {"count_logical": None, "count_physical": None}
        try:
            freq = psutil.cpu_freq()
        except Exception:
            freq = None
        return {
            "count_logical": psutil.cpu_count(logical=True),
            "count_physical": psutil.cpu_count(logical=False),
            "freq_current_mhz": getattr(freq, "current", None) if freq else None,
            "freq_max_mhz": getattr(freq, "max", None) if freq else None,
            "percent": psutil.cpu_percent(interval=0.1),
            "arch": platform.machine(),
        }

    def _ram(self) -> dict[str, Any]:
        if not _HAS_PSUTIL:
            return {"total_bytes": None}
        vm = psutil.virtual_memory()
        return {
            "total_bytes": int(vm.total),
            "available_bytes": int(vm.available),
            "used_bytes": int(vm.used),
            "percent": float(vm.percent),
        }

    def _disks(self) -> list[dict[str, Any]]:
        disks: list[dict[str, Any]] = []
        if not _HAS_PSUTIL:
            return disks
        try:
            for p in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(p.mountpoint)
                    disks.append({
                        "device": p.device,
                        "mountpoint": p.mountpoint,
                        "fstype": p.fstype,
                        "total_bytes": int(u.total),
                        "used_bytes": int(u.used),
                        "free_bytes": int(u.free),
                        "percent": float(u.percent),
                    })
                except Exception:
                    continue
        except Exception as e:
            logger.warning("disk_enumeration_failed", error=str(e))
        return disks

    def _uptime(self) -> int | None:
        if not _HAS_PSUTIL:
            return None
        try:
            return int(time.time() - psutil.boot_time())
        except Exception:
            return None

    # ── network probes ───────────────────────────────────
    def _network_probe_sync(
        self, interfaces: list[str], subnets: list[str],
    ) -> dict[str, Any]:
        results: dict[str, Any] = {
            "interfaces": {},
            "subnets": {},
            "mdns": self._test_mdns(),
            "upnp": self._test_upnp(),
        }
        for ifname in interfaces:
            results["interfaces"][ifname] = self._probe_interface(ifname)
        for subnet in subnets:
            results["subnets"][subnet] = self._probe_subnet(subnet)
        return results

    def _probe_interface(self, ifname: str) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False}
        try:
            # Find broadcast address from psutil
            if _HAS_PSUTIL:
                addrs = psutil.net_if_addrs().get(ifname, [])
                ipv4 = next((a for a in addrs
                            if getattr(a, "family", None) == socket.AF_INET), None)
                if not ipv4:
                    return {"ok": False, "error": "no IPv4 on interface"}
                bcast = getattr(ipv4, "broadcast", None) or "255.255.255.255"
                # Open a UDP socket and try a broadcast send.
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(1.0)
                try:
                    s.sendto(b"helen-onboarding-probe", (bcast, 0))
                    out["broadcast_sent_to"] = bcast
                    out["ok"] = True
                finally:
                    s.close()
            else:
                out["error"] = "psutil unavailable"
        except Exception as e:
            out["error"] = str(e)
        return out

    def _probe_subnet(self, subnet: str) -> dict[str, Any]:
        try:
            import ipaddress
            net = ipaddress.ip_network(subnet, strict=False)
            return {
                "ok": True,
                "network": str(net.network_address),
                "prefix": net.prefixlen,
                "num_addresses": net.num_addresses,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _test_mdns(self) -> dict[str, Any]:
        """Best-effort multicast DNS probe — fails open."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            # Minimal mDNS query for _services._dns-sd._udp.local
            packet = (
                b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                b"\x09_services\x07_dns-sd\x04_udp\x05local\x00\x00\x0c\x00\x01"
            )
            try:
                s.sendto(packet, ("224.0.0.251", 5353))
                return {"ok": True, "sent": True}
            finally:
                s.close()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _test_upnp(self) -> dict[str, Any]:
        """SSDP M-SEARCH broadcast for UPnP IGD."""
        try:
            ssdp_req = (
                "M-SEARCH * HTTP/1.1\r\n"
                "HOST: 239.255.255.250:1900\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 1\r\n"
                "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
                "\r\n"
            ).encode()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.5)
            try:
                s.sendto(ssdp_req, ("239.255.255.250", 1900))
                responders: list[str] = []
                end_at = time.time() + 1.2
                while time.time() < end_at:
                    try:
                        _data, addr = s.recvfrom(2048)
                        responders.append(addr[0])
                    except socket.timeout:
                        break
                    except Exception:
                        break
                return {"ok": True, "responders": responders}
            finally:
                s.close()
        except Exception as e:
            return {"ok": False, "error": str(e)}
