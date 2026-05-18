"""Per-NIC routing table — picks the best local NIC for a destination.

When the host has multiple network interfaces (Ethernet + WiFi +
USB-tether + fiber), naive routing always uses the OS default. This
table inspects each NIC's IP/subnet and chooses the one whose subnet
matches the destination — falling back to the OS default when no
NIC is on the same subnet.

Pure-data; no socket plumbing here. The cluster_mesh / multipath
layer queries this table when it has multiple equally-good paths.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class NICEntry:
    name:       str
    ip:         str
    subnet:     str   # CIDR
    is_up:      bool
    speed_mbps: int
    last_seen:  float


class NICRoutingTable:
    _singleton: "NICRoutingTable | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nics: dict[str, NICEntry] = {}
        self._refreshed_at: float = 0.0

    @classmethod
    def instance(cls) -> "NICRoutingTable":
        if cls._singleton is None:
            cls._singleton = NICRoutingTable()
        return cls._singleton

    # ── Refresh ───────────────────────────────────────────

    def refresh(self) -> int:
        """Re-enumerate local interfaces. Returns count discovered."""
        new: dict[str, NICEntry] = {}
        try:
            import psutil
            stats = psutil.net_if_stats()
            addrs = psutil.net_if_addrs()
            for name, addr_list in addrs.items():
                stat = stats.get(name)
                is_up = bool(stat and stat.isup)
                speed = int(stat.speed) if stat else 0
                for a in addr_list:
                    if a.family.name != "AF_INET":
                        continue
                    if not a.address or a.address.startswith("169.254."):
                        continue
                    try:
                        net = ipaddress.ip_network(
                            f"{a.address}/{a.netmask or '24'}",
                            strict=False,
                        )
                    except ValueError:
                        continue
                    new[f"{name}@{a.address}"] = NICEntry(
                        name=name, ip=a.address,
                        subnet=str(net), is_up=is_up,
                        speed_mbps=speed, last_seen=time.time(),
                    )
        except Exception:
            pass

        with self._lock:
            self._nics = new
            self._refreshed_at = time.time()
        return len(new)

    # ── Query ─────────────────────────────────────────────

    def best_nic_for(self, dst_ip: str) -> Optional[NICEntry]:
        """Return the NIC whose subnet contains dst_ip, preferring
        UP interfaces with the highest speed."""
        if not dst_ip:
            return None
        with self._lock:
            entries = list(self._nics.values())
        if not entries:
            self.refresh()
            with self._lock:
                entries = list(self._nics.values())

        try:
            target = ipaddress.ip_address(dst_ip)
        except ValueError:
            return None

        candidates: list[NICEntry] = []
        for n in entries:
            try:
                if target in ipaddress.ip_network(n.subnet, strict=False):
                    candidates.append(n)
            except ValueError:
                continue

        if not candidates:
            return None
        # Prefer UP, faster.
        candidates.sort(
            key=lambda n: (1 if n.is_up else 0, n.speed_mbps),
            reverse=True,
        )
        return candidates[0]

    def all_nics(self) -> list[NICEntry]:
        with self._lock:
            return list(self._nics.values())

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "refreshed_at": self._refreshed_at,
                "count":        len(self._nics),
                "nics": [
                    {
                        "name":       n.name,
                        "ip":         n.ip,
                        "subnet":     n.subnet,
                        "is_up":      n.is_up,
                        "speed_mbps": n.speed_mbps,
                    }
                    for n in self._nics.values()
                ],
            }


def get_nic_routing_table() -> NICRoutingTable:
    return NICRoutingTable.instance()
