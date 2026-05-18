"""Peer capabilities — read-and-advertise hardware metadata.

Wraps ``distributed_system.node_capabilities`` so the p2p layer
never imports across packages directly.
"""

from __future__ import annotations


def detect_local_capabilities() -> dict:
    try:
        from app.distributed_system.node_capabilities import detect_local
        return detect_local().to_dict()
    except Exception:
        return {"cpu_cores": 1, "ram_gb": 1.0, "nic_gbps": 1.0,
                "disk_ssd": True, "platform": "?", "version": "?"}


def supports_role(caps: dict, role: str) -> bool:
    """Heuristic: does this peer's hardware support a given role?

      * SUPER     — needs ≥ 4 cores AND ≥ 8 GB RAM
      * SFU       — needs ≥ 4 cores AND ≥ 1 Gbps NIC
      * STORAGE   — needs SSD
      * RELAY     — anything works
    """
    cores  = int(caps.get("cpu_cores") or 1)
    ram    = float(caps.get("ram_gb") or 1.0)
    nic    = float(caps.get("nic_gbps") or 1.0)
    ssd    = bool(caps.get("disk_ssd") or False)
    role_l = (role or "").lower()
    if role_l == "super":
        return cores >= 4 and ram >= 8.0
    if role_l == "sfu":
        return cores >= 4 and nic >= 1.0
    if role_l == "storage":
        return ssd
    if role_l == "relay":
        return True
    return True
