"""Node capabilities — hardware advertisement.

Centralised so every layer (placement, scoring, capacity planning)
asks the same question through the same interface.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass


@dataclass
class NodeCapabilities:
    cpu_cores: int
    ram_gb:    float
    nic_gbps:  float
    disk_ssd:  bool
    platform:  str
    version:   str

    def strength(self) -> float:
        """Single-number weight used by routing scorers — same formula
        as services.node_registry.compute_strength for consistency."""
        return round(
            0.4 * self.cpu_cores +
            0.3 * self.ram_gb +
            0.2 * (self.nic_gbps * 10) +
            0.1 * (5 if self.disk_ssd else 1),
            2,
        )

    def to_dict(self) -> dict:
        return {
            "cpu_cores": self.cpu_cores,
            "ram_gb":    self.ram_gb,
            "nic_gbps":  self.nic_gbps,
            "disk_ssd":  self.disk_ssd,
            "platform":  self.platform,
            "version":   self.version,
            "strength":  self.strength(),
        }


def detect_local() -> NodeCapabilities:
    cores = 1
    ram_gb = 1.0
    nic_gbps = 1.0
    try:
        import psutil
        cores = psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 1
        ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        stats = psutil.net_if_stats()
        speeds = [s.speed for n, s in stats.items()
                  if s.isup and not n.lower().startswith(("lo", "loopback"))
                  and s.speed > 0]
        if speeds:
            nic_gbps = round(max(speeds) / 1000.0, 2)
    except Exception:
        pass
    return NodeCapabilities(
        cpu_cores=int(cores),
        ram_gb=ram_gb,
        nic_gbps=nic_gbps,
        disk_ssd=True,
        platform=f"{platform.system()} {platform.release()}",
        version="1.0.0",
    )
