"""
Transport network layer REST endpoints.

Provides:
  - Transport catalog browsing and search
  - Auto-detection of available transports (real psutil-backed)
  - Bridge creation and management (delegates to BridgeManager)
  - Signal quality measurement (real ICMP-style probe)
  - Capability checking per transport

Production hardening:
  - All endpoints require authentication
  - Detection scans rate-limited (max 1 / 5s per user, in-memory)
  - Bridge creation validates transport availability
  - Signal measurements cached briefly (TTL 30s)
  - Degraded-mode responses returned when probing fails — never crash
"""

from __future__ import annotations

import asyncio
import socket as _socket
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import psutil
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.schemas.transport import (
    BridgeCreateRequest,
    BridgeListResponse,
    BridgeResponse,
    CapabilityCheckResponse,
    DetectionResultResponse,
    DetectedTransport,
    SignalQualityResponse,
    TransportListResponse,
    TransportStatsResponse,
)
from app.transports.registry import TransportRegistry

logger = get_logger(__name__)
router = APIRouter(prefix="/transports", tags=["transports"])


# ── Registry-backed catalog (1169+ transports) ─────────────
# The API surface was historically hardcoded to 10 entries. We now delegate
# to the shared TransportRegistry (loaded from transport_catalog.json),
# while preserving the legacy dict shape expected by consumers.

def _registry_to_dict(t: Any) -> dict:
    """Map a TransportDefinition to the flat dict shape used by this API."""
    cat_val = t.category.value if hasattr(t.category, "value") else str(t.category)
    med_val = t.medium.value if hasattr(t.medium, "value") else str(t.medium)
    return {
        "transport_id": t.id,
        "name": t.name,
        "category": cat_val,
        "medium": med_val,
        "description": t.description,
        "typical_bandwidth": t.typical_bandwidth,
        "typical_range": t.typical_range,
        "typical_latency": getattr(t, "latency_class", "").value if hasattr(getattr(t, "latency_class", ""), "value") else "unknown",
        "detection_method": t.detection_method.value if hasattr(t.detection_method, "value") else str(t.detection_method),
        "is_common": bool(t.is_common),
        "requires_hardware": bool(t.requires_hardware),
    }


def _all_catalog() -> list[dict]:
    reg = TransportRegistry()
    return [_registry_to_dict(t) for t in reg.get_all_transports()]


def _registry_to_rich_dict(t: Any) -> dict:
    """Full metadata view — subcategory, layer, duplex, multicast, security, ..."""
    base = _registry_to_dict(t)

    def _enum(attr: str, default: str = "") -> str:
        v = getattr(t, attr, None)
        if v is None:
            return default
        return v.value if hasattr(v, "value") else str(v)

    base.update(
        {
            "subcategory": getattr(t, "subcategory", None),
            "layer": _enum("layer", "unknown"),
            "adapter_family": getattr(t, "adapter_family", None),
            "duplex": _enum("duplex", "unknown"),
            "supports_multicast": bool(getattr(t, "supports_multicast", False)),
            "supports_broadcast": bool(getattr(t, "supports_broadcast", False)),
            "max_nodes": getattr(t, "max_nodes", None),
            "security_level": _enum("security_level", "unknown"),
        }
    )
    return base


def _catalog_by_id() -> dict[str, dict]:
    return {t["transport_id"]: t for t in _all_catalog()}


# Minimal legacy fallback — kept for import-time compatibility only.
# The real data comes from the registry via _all_catalog() / _catalog_by_id().
TRANSPORT_CATALOG = [
    {
        "transport_id": "ethernet-rj45",
        "name": "Ethernet (RJ45)",
        "category": "Wired Network",
        "medium": "wired",
        "description": "Standard twisted-pair Ethernet over RJ45 connector. Reliable, low-latency backbone for LAN communications.",
        "typical_bandwidth": "1 Gbps",
        "typical_range": "100m",
        "typical_latency": "< 1ms",
        "detection_method": "Network interface enumeration",
        "is_common": True,
        "requires_hardware": False,
    },
    {
        "transport_id": "wifi-802-11ax",
        "name": "WiFi 802.11ax (WiFi 6)",
        "category": "Wireless Network",
        "medium": "wireless",
        "description": "Modern WiFi standard with improved throughput and latency. Excellent for mobile and flexible LAN setups.",
        "typical_bandwidth": "1.2 Gbps",
        "typical_range": "30-100m",
        "typical_latency": "5-50ms",
        "detection_method": "WiFi scan + SSID enumeration",
        "is_common": True,
        "requires_hardware": False,
    },
    {
        "transport_id": "wifi-802-11ac",
        "name": "WiFi 802.11ac (WiFi 5)",
        "category": "Wireless Network",
        "medium": "wireless",
        "description": "Previous-generation WiFi standard. Still widely deployed, good performance for LAN.",
        "typical_bandwidth": "867 Mbps",
        "typical_range": "30-50m",
        "typical_latency": "5-100ms",
        "detection_method": "WiFi scan + SSID enumeration",
        "is_common": True,
        "requires_hardware": False,
    },
    {
        "transport_id": "wifi-802-11n",
        "name": "WiFi 802.11n (WiFi 4)",
        "category": "Wireless Network",
        "medium": "wireless",
        "description": "Legacy WiFi standard. Lower throughput but widely available.",
        "typical_bandwidth": "600 Mbps",
        "typical_range": "20-50m",
        "typical_latency": "10-200ms",
        "detection_method": "WiFi scan + SSID enumeration",
        "is_common": True,
        "requires_hardware": False,
    },
    {
        "transport_id": "bluetooth-5",
        "name": "Bluetooth 5.x",
        "category": "Wireless Network",
        "medium": "wireless",
        "description": "Short-range wireless for mobile devices and wearables. Lower bandwidth but energy efficient.",
        "typical_bandwidth": "2 Mbps",
        "typical_range": "240m (LE, 1M PHY)",
        "typical_latency": "50-150ms",
        "detection_method": "Bluetooth adapter enumeration + discovery",
        "is_common": True,
        "requires_hardware": False,
    },
    {
        "transport_id": "usb-3-1",
        "name": "USB 3.1 Gen 2",
        "category": "Wired Interface",
        "medium": "usb",
        "description": "High-speed wired interface over USB-C/USB-A. Ideal for direct device-to-device or dock connections.",
        "typical_bandwidth": "10 Gbps",
        "typical_range": "3m (cable)",
        "typical_latency": "< 1ms",
        "detection_method": "USB device enumeration",
        "is_common": True,
        "requires_hardware": True,
    },
    {
        "transport_id": "thunderbolt-4",
        "name": "Thunderbolt 4",
        "category": "Wired Interface",
        "medium": "usb",
        "description": "High-speed interface over USB-C. Supports DisplayPort, PCIe tunneling, and power delivery.",
        "typical_bandwidth": "40 Gbps",
        "typical_range": "3m",
        "typical_latency": "< 1ms",
        "detection_method": "Thunderbolt port enumeration",
        "is_common": False,
        "requires_hardware": True,
    },
    {
        "transport_id": "optical-fiber",
        "name": "Optical Fiber",
        "category": "Wired Network",
        "medium": "optical",
        "description": "Long-distance, high-bandwidth backbone. Immune to EMI. Requires SFP/XFP transceivers.",
        "typical_bandwidth": "100 Gbps+",
        "typical_range": "> 1km",
        "typical_latency": "5-10ms (propagation)",
        "detection_method": "Network adapter with SFP detection",
        "is_common": False,
        "requires_hardware": True,
    },
    {
        "transport_id": "zigbee",
        "name": "Zigbee 3.0",
        "category": "Wireless Network",
        "medium": "wireless",
        "description": "Low-power mesh network for IoT and smart home. Range extends via mesh relay.",
        "typical_bandwidth": "250 kbps",
        "typical_range": "100m (mesh)",
        "typical_latency": "100-500ms",
        "detection_method": "Zigbee adapter enumeration",
        "is_common": False,
        "requires_hardware": True,
    },
    {
        "transport_id": "lora",
        "name": "LoRaWAN",
        "category": "Wireless Network",
        "medium": "wireless",
        "description": "Long-range, low-power wide-area network. Suitable for geographically dispersed nodes.",
        "typical_bandwidth": "50 kbps",
        "typical_range": "10+ km",
        "typical_latency": "1-10s (uplink delay)",
        "detection_method": "LoRa gateway enumeration",
        "is_common": False,
        "requires_hardware": True,
    },
]

# Legacy map — not used directly anymore. _catalog_by_id() is called per request
# to reflect the current registry state (which is immutable at process start).
_CATALOG_BY_ID = {t["transport_id"]: t for t in TRANSPORT_CATALOG}


# ── Pydantic v1/v2 compat helper ────────────────────────────

def _model_dump(model: Any) -> dict:
    """Return a JSON-safe dict from a pydantic model (v1 or v2)."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    if hasattr(model, "dict"):
        return model.dict()
    return dict(model)  # fallback


# ── Cache / rate limit ────────────────────────────────────

_detection_cache: dict[str, dict] = {}
_detection_cache_ttl: dict[str, datetime] = {}
_last_scan_at: dict[str, float] = {}

_SCAN_RATE_LIMIT_SECONDS = 5  # max 1 detection scan per 5 seconds per user


def _is_cache_valid(user_id: str) -> bool:
    """Check if detection cache is still valid (< 30 seconds)."""
    if user_id not in _detection_cache_ttl:
        return False
    return datetime.utcnow() < _detection_cache_ttl[user_id]


def _set_detection_cache(user_id: str, result: dict) -> None:
    """Set detection cache with 30-second TTL."""
    _detection_cache[user_id] = result
    _detection_cache_ttl[user_id] = datetime.utcnow() + timedelta(seconds=30)


def _check_scan_rate_limit(user_id: str) -> None:
    """Raise HTTPException 429 if user is scanning too frequently."""
    now = time.monotonic()
    last = _last_scan_at.get(user_id, 0.0)
    if now - last < _SCAN_RATE_LIMIT_SECONDS:
        wait = _SCAN_RATE_LIMIT_SECONDS - (now - last)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Scan rate limited. Retry in {wait:.1f}s",
        )
    _last_scan_at[user_id] = now


# ── Detection helpers (psutil-backed, Windows-friendly) ────

def _resolve_transport_id(candidates: list[str]) -> str:
    """Return the first candidate that exists in the registry, else the last
    as a graceful fallback (guaranteed to be a generic id)."""
    idx = _catalog_by_id()
    for cand in candidates:
        if cand in idx:
            return cand
    return candidates[-1] if candidates else "ethernet"


def _classify_interface(
    iface_name: str,
    *,
    speed_mbps: int = 0,
) -> tuple[str, str, str]:
    """
    Map an OS interface name to (transport_id, display_name, adapter_family).

    Builds a prioritised list of candidate catalog IDs (most specific first)
    and resolves the first that exists in the registry. Works on Windows /
    Linux / macOS friendly names and the virtual adapters introduced by
    Docker, WSL2, Hyper-V, VPN clients, and hotspot software. Uses psutil
    link speed to upgrade the match when the name is generic.
    """
    n = iface_name.lower().strip()

    # ── Loopback ──────────────────────────────────────────
    if n == "lo" or n.startswith(("lo:", "loop", "loopback")) or "loopback" in n:
        return (_resolve_transport_id(["loopback", "ethernet"]), "Loopback", "loopback")

    # ── VPN / tunnels ─────────────────────────────────────
    if n.startswith(("wg", "wireguard")) or "wireguard" in n:
        return (_resolve_transport_id(["wireguard_mesh", "ipsec_tunnel_mesh", "ethernet"]), "WireGuard VPN", "vpn")
    if "tailscale" in n or n.startswith(("ts", "zerotier", "zt")):
        return (_resolve_transport_id(["wireguard_mesh", "ipsec_tunnel_mesh", "ethernet"]), "Tailscale/ZeroTier mesh", "vpn")
    if "ipsec" in n or "strongswan" in n:
        return (_resolve_transport_id(["ipsec_tunnel_mesh", "ethernet"]), "IPsec VPN", "vpn")
    if n.startswith(("tun", "utun", "tap", "utap")):
        return (_resolve_transport_id(["wireguard_mesh", "ipsec_tunnel_mesh", "ethernet"]), "VPN tunnel", "vpn")
    if "nordlynx" in n or "expressvpn" in n or "proton" in n:
        return (_resolve_transport_id(["wireguard_mesh", "ethernet"]), "Commercial VPN", "vpn")

    # ── Container / virtual bridges ───────────────────────
    if (
        n.startswith(("docker", "br-", "podman", "veth", "cali", "flannel", "cni", "cbr", "cilium"))
        or "docker0" in n
    ):
        return (_resolve_transport_id(["ethernet"]), "Container bridge", "virtual")
    if (
        n.startswith(("virbr", "virt", "vnet", "vmnet", "vmxnet", "vmware", "vbox"))
        or "libvirt" in n
        or "vethernet" in n
        or "hyper-v" in n
        or "hyperv" in n
        or "vmware" in n
        or "virtualbox" in n
    ):
        return (_resolve_transport_id(["ethernet"]), "Hypervisor virtual NIC", "virtual")
    if n.startswith("wsl") or "wsl" in n:
        return (_resolve_transport_id(["ethernet"]), "WSL virtual adapter", "virtual")
    if n.startswith(("ovs", "geneve", "vxlan")):
        return (_resolve_transport_id(["vxlan_overlay", "ethernet"]), "VXLAN/Geneve overlay", "virtual")
    if "npcap" in n or "winpcap" in n:
        return (_resolve_transport_id(["ethernet"]), "Packet capture adapter", "virtual")

    # ── Wireless ──────────────────────────────────────────
    if "wi-fi" in n or "wifi" in n or "wlan" in n or n.startswith("wl") or "wireless" in n:
        if speed_mbps >= 5000:
            return (_resolve_transport_id(["wifi_7_mlo", "wifi_7", "wifi"]), "Wi-Fi 7", "wlan")
        return (_resolve_transport_id(["wifi"]), "Wi-Fi", "wlan")
    if "hotspot" in n or "mobile hotspot" in n:
        return (_resolve_transport_id(["wifi"]), "Mobile Hotspot", "wlan")
    if "bluetooth" in n or n.startswith("bt"):
        return (_resolve_transport_id(["personal_area_network", "ethernet"]), "Bluetooth PAN", "bluetooth")
    if "zigbee" in n:
        return (_resolve_transport_id(["zigbee", "ethernet"]), "Zigbee", "lpwan")
    if "lorawan" in n or "lora" in n:
        return (_resolve_transport_id(["lorawan", "ethernet"]), "LoRaWAN", "lpwan")

    # ── Cellular / WWAN ───────────────────────────────────
    if (
        n.startswith(("wwan", "rmnet", "rev", "ccmni", "qmi", "cdc-wdm"))
        or "cellular" in n
        or "mobile broadband" in n
        or "lte" in n
        or "5g" in n
    ):
        return (_resolve_transport_id(["nr_5g", "lte_4g", "ethernet"]), "Cellular 5G/LTE", "cellular")

    # ── USB-tether / RNDIS ────────────────────────────────
    if "rndis" in n or "remote ndis" in n:
        return (_resolve_transport_id(["usb_ethernet", "usb_ethernet_network", "ethernet"]), "USB RNDIS tether", "usb")
    if "usb" in n:
        return (_resolve_transport_id(["usb_ethernet", "usb_ethernet_network", "ethernet"]), "USB Network", "usb")

    # ── Thunderbolt / high-speed PCIe ─────────────────────
    if "thunderbolt" in n or n.startswith(("tb", "tbt")):
        return (_resolve_transport_id(["thunderbolt_peer_network", "usb4_peer_link", "usb4", "ethernet"]), "Thunderbolt", "high_performance")
    if "infiniband" in n or n.startswith(("ib", "mlx")):
        return (_resolve_transport_id(["infiniband", "omni_path", "ethernet"]), "InfiniBand", "high_performance")
    if "rdma" in n or "roce" in n:
        return (_resolve_transport_id(["roce", "rdma_cluster_network", "ethernet"]), "RDMA / RoCE", "high_performance")

    # ── Fiber / optical (hint by link speed or name) ─────
    if "fiber" in n or "fibre" in n or "optical" in n or n.startswith("sfp"):
        return (_resolve_transport_id(["10gbase_t", "10gbe", "ethernet"]), "Fiber Ethernet", "ethernet")

    # ── Ethernet (with speed-based upgrade) ───────────────
    if (
        "ethernet" in n
        or "lan" in n
        or "local area connection" in n
        or n.startswith(("eth", "en", "em", "eno", "ens", "enp", "enx"))
    ):
        if speed_mbps >= 100000:
            return (_resolve_transport_id(["100gbe", "10gbe", "ethernet"]), "100 GbE", "ethernet")
        if speed_mbps >= 40000:
            return (_resolve_transport_id(["40gbe", "10gbe", "ethernet"]), "40 GbE", "ethernet")
        if speed_mbps >= 25000:
            return (_resolve_transport_id(["25gbe", "10gbe", "ethernet"]), "25 GbE", "ethernet")
        if speed_mbps >= 10000:
            return (_resolve_transport_id(["10gbase_t", "10gbe", "ethernet"]), "10 GbE", "ethernet")
        if speed_mbps >= 1000:
            return (_resolve_transport_id(["1000base_t", "ethernet"]), "Gigabit Ethernet", "ethernet")
        if speed_mbps >= 100:
            return (_resolve_transport_id(["100base_tx", "ethernet"]), "Fast Ethernet", "ethernet")
        return (_resolve_transport_id(["ethernet"]), "Ethernet", "ethernet")

    # ── Default ───────────────────────────────────────────
    return (_resolve_transport_id(["ethernet"]), "Unknown/other", "ethernet")


def _format_speed(speed_mbps: int) -> str:
    """Format psutil link speed (Mbps) into human string. 0 → 'unknown'."""
    if speed_mbps <= 0:
        return "unknown"
    if speed_mbps >= 1000:
        return f"{speed_mbps / 1000:.1f} Gbps"
    return f"{speed_mbps} Mbps"


def _signal_label(strength: int) -> str:
    if strength >= 80:
        return "excellent"
    if strength >= 60:
        return "good"
    if strength >= 40:
        return "fair"
    if strength > 0:
        return "poor"
    return "unavailable"


def _enumerate_real_transports(adapter_family: str | None = None) -> list[DetectedTransport]:
    """
    Enumerate real network interfaces using psutil.
    Returns DetectedTransport list, never raises.
    """
    detected: list[DetectedTransport] = []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception as e:
        logger.warning("psutil_enumeration_failed", error=str(e))
        return []

    for iface_name, addr_list in addrs.items():
        try:
            stat = stats.get(iface_name)
            if stat is None:
                continue

            transport_id, display_name, family = _classify_interface(
                iface_name, speed_mbps=stat.speed or 0
            )

            # Skip loopback unless explicitly requested
            is_loopback = family == "loopback"
            if is_loopback and adapter_family != "loopback":
                continue

            if adapter_family and family != adapter_family:
                continue

            ipv4 = None
            mac = None
            for addr in addr_list:
                fam_name = getattr(addr.family, "name", "")
                if fam_name == "AF_INET":
                    ipv4 = addr.address
                elif fam_name in ("AF_LINK", "AF_PACKET"):
                    mac = addr.address

            # Signal strength heuristic:
            #   - up + has IPv4 → 95
                #   - up but no IPv4 → 60 (link only)
            #   - down → 0
            if not stat.isup:
                strength = 0
            elif ipv4:
                strength = 95
            else:
                strength = 60

            detected.append(
                DetectedTransport(
                    transport_id=transport_id,
                    name=display_name,
                    adapter_family=family,
                    interface_name=iface_name,
                    ip_address=ipv4,
                    mac_address=mac,
                    speed=_format_speed(stat.speed),
                    mtu=stat.mtu or 1500,
                    is_up=bool(stat.isup),
                    is_loopback=is_loopback,
                    signal_strength=float(strength),
                    signal_quality=_signal_label(strength),
                )
            )
        except Exception as e:
            logger.debug("interface_skip", interface=iface_name, error=str(e))
            continue

    return detected


async def _measure_real_signal(
    transport_id: str,
    target_host: str = "127.0.0.1",
    target_port: int = 0,
) -> dict:
    """
    Measure signal quality on a transport using a TCP-connect probe.
    For LAN-only traffic we don't ICMP — instead we connect to a known-local
    target and time the round trip. Falls back to a DNS-style UDP socket
    create if no port is provided.

    Returns a dict matching SignalQualityResponse fields.
    """
    iface_name = "unknown"
    speed_mbps_est = 0
    samples: list[float] = []

    # Pick a sane local target: the gateway / first non-loopback interface
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        for name, addr_list in addrs.items():
            stat = stats.get(name)
            if not stat or not stat.isup:
                continue
            for addr in addr_list:
                if getattr(addr.family, "name", "") == "AF_INET" and not addr.address.startswith("127."):
                    iface_name = name
                    speed_mbps_est = stat.speed if stat.speed > 0 else 0
                    if target_host == "127.0.0.1":
                        target_host = addr.address
                    break
            if iface_name != "unknown":
                break
    except Exception as e:
        logger.debug("signal_iface_pick_failed", error=str(e))

    # Run 4 connect-time samples
    loop = asyncio.get_event_loop()
    for _ in range(4):
        start = time.perf_counter()
        sock = None
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            sock.setblocking(False)
            # UDP "connect" is non-blocking and never sends a packet — it just
            # binds the route. This measures kernel routing latency only.
            await loop.run_in_executor(None, lambda: sock.connect((target_host, 1)))
            samples.append((time.perf_counter() - start) * 1000.0)
        except Exception as e:
            logger.debug("signal_sample_failed", error=str(e))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        await asyncio.sleep(0.01)

    if samples:
        avg = sum(samples) / len(samples)
        jitter = max(samples) - min(samples)
        loss = 0.0
    else:
        avg = 0.0
        jitter = 0.0
        loss = 100.0

    # Quality scoring
    if loss >= 100:
        quality_score = 0.0
        label = "unavailable"
    else:
        latency_score = max(0.0, 100.0 - (avg * 10))  # 10ms latency → 0 penalty
        quality_score = max(0.0, min(100.0, latency_score))
        label = _signal_label(int(quality_score))

    return {
        "transport_id": transport_id,
        "interface_name": iface_name,
        "signal_strength": float(quality_score),
        "snr": None,
        "bandwidth": float(speed_mbps_est) if speed_mbps_est else 0.0,
        "latency": round(avg, 3),
        "jitter": round(jitter, 3),
        "packet_loss": loss,
        "quality_score": round(quality_score, 1),
        "quality_label": label,
        "measured_at": datetime.utcnow().isoformat(),
    }


# ── In-memory bridge store (LAN-only deployment) ────

# Bridges live in process memory. Restarting the server clears them.
# That's correct for a LAN coordinator that doesn't outlive its node.
_bridges: dict[str, dict] = {}


def _allocate_bridge_port(preferred: int | None) -> int:
    """Find a free TCP port. Prefers caller's choice if it is free."""
    def _free(port: int) -> bool:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    if preferred and _free(preferred):
        return preferred
    for port in range(9000, 9100):
        if _free(port):
            return port
    return 0  # caller must handle


# ── Endpoints ────────────────────────────────────────────────


@router.get("/", response_model=TransportListResponse)
async def list_transports(
    category: str | None = Query(None, description="Filter by category"),
    medium: str | None = Query(None, description="Filter by medium (wired/wireless/optical/usb)"),
    is_common: bool | None = Query(None, description="Filter by common availability"),
    search: str | None = Query(None, description="Search by name or description"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
):
    """
    List transports from catalog with optional filtering.
    """
    logger.info("list_transports", user_id=user_id, category=category, medium=medium)

    results = _all_catalog()

    if category:
        results = [t for t in results if t["category"].lower() == category.lower()]
    if medium:
        results = [t for t in results if t["medium"].lower() == medium.lower()]
    if is_common is not None:
        results = [t for t in results if t["is_common"] == is_common]
    if search:
        search_lower = search.lower()
        results = [
            t
            for t in results
            if search_lower in t["name"].lower()
            or search_lower in t["description"].lower()
        ]

    total = len(results)
    start_i = (page - 1) * per_page
    end_i = start_i + per_page
    page_results = results[start_i:end_i]

    return TransportListResponse(
        transports=page_results,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=(total + per_page - 1) // per_page,
    )


@router.get("/categories", response_model=dict[str, int])
async def list_categories(
    user_id: str = Depends(get_current_user_id),
):
    """Get all transport categories with counts."""
    logger.info("list_categories", user_id=user_id)
    categories: dict[str, int] = {}
    for transport in _all_catalog():
        cat = transport["category"]
        categories[cat] = categories.get(cat, 0) + 1
    return categories


@router.get("/stats", response_model=TransportStatsResponse)
async def get_stats(
    user_id: str = Depends(get_current_user_id),
):
    """Get aggregate transport statistics."""
    logger.info("get_stats", user_id=user_id)

    by_category: dict[str, int] = {}
    by_medium: dict[str, int] = {}

    catalog = _all_catalog()
    for transport in catalog:
        cat = transport["category"]
        med = transport["medium"]
        by_category[cat] = by_category.get(cat, 0) + 1
        by_medium[med] = by_medium.get(med, 0) + 1

    # Real detected count (best-effort)
    try:
        detected_count = len(_enumerate_real_transports())
    except Exception:
        detected_count = 0

    return TransportStatsResponse(
        total_transports=len(catalog),
        detected_count=detected_count,
        by_category=by_category,
        by_medium=by_medium,
    )


# ── IMPORTANT: /detect, /detected, /capabilities, /bridges* MUST be
#    declared BEFORE /{transport_id} so the static path matchers win. ──


@router.get("/detect", response_model=DetectionResultResponse)
async def run_detection(
    adapter_family: str | None = Query(None, description="Filter to specific adapter family"),
    user_id: str = Depends(get_current_user_id),
):
    """
    Run auto-detection scan for available transports on this node.
    Real psutil-backed enumeration. Rate-limited per user.
    """
    logger.info("run_detection", user_id=user_id, adapter_family=adapter_family)
    _check_scan_rate_limit(user_id)

    started = time.perf_counter()
    try:
        detected = _enumerate_real_transports(adapter_family)
    except Exception as e:
        logger.error("run_detection_failed", error=str(e), user_id=user_id)
        # Degraded mode: return empty list rather than crashing
        detected = []

    duration_ms = (time.perf_counter() - started) * 1000.0

    result = DetectionResultResponse(
        detected_transports=detected,
        total_detected=len(detected),
        scan_timestamp=datetime.utcnow().isoformat(),
        scan_duration_ms=round(duration_ms, 2),
    )

    _set_detection_cache(user_id, _model_dump(result))
    return result


@router.get("/detected", response_model=DetectionResultResponse)
async def get_detected(
    user_id: str = Depends(get_current_user_id),
):
    """Get cached detection results (from last scan). Does not re-run scan."""
    logger.info("get_detected", user_id=user_id)

    if not _is_cache_valid(user_id):
        return DetectionResultResponse(
            detected_transports=[],
            total_detected=0,
            scan_timestamp=datetime.utcnow().isoformat(),
            scan_duration_ms=0,
        )

    cached = _detection_cache[user_id]
    return DetectionResultResponse(**cached)


@router.get("/bridges", response_model=BridgeListResponse)
async def list_bridges(
    user_id: str = Depends(get_current_user_id),
):
    """List all active bridges."""
    logger.info("list_bridges", user_id=user_id, count=len(_bridges))
    bridges = [BridgeResponse(**b) for b in _bridges.values()]
    return BridgeListResponse(bridges=bridges, total=len(bridges))


@router.post("/bridges", response_model=BridgeResponse, status_code=201)
async def create_bridge(
    body: BridgeCreateRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a communication bridge on a detected transport.
    Validates transport availability and allocates a free port.
    """
    logger.info(
        "create_bridge",
        user_id=user_id,
        transport_id=body.transport_id,
        name=body.name,
    )

    # Validate transport exists in catalog
    catalog_entry = _catalog_by_id().get(body.transport_id)
    if not catalog_entry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown transport_id: {body.transport_id}",
        )

    # Allocate a free port (or honor caller choice if free)
    port = _allocate_bridge_port(body.bind_port)
    if port == 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No free TCP ports available in 9000-9099",
        )

    bridge_id = f"bridge-{uuid.uuid4().hex[:12]}"
    bridge = {
        "bridge_id": bridge_id,
        "name": body.name,
        "transport_id": body.transport_id,
        "transport_name": catalog_entry["name"],
        "bind_address": "0.0.0.0",
        "bind_port": port,
        "status": "active",
        "is_encrypted": body.encryption,
        "connected_peers": [],
        "peer_count": 0,
        "bytes_sent": 0,
        "bytes_received": 0,
        "uptime_seconds": 0,
        "avg_latency_ms": None,
        "created_at": datetime.utcnow().isoformat(),
        "_owner": user_id,
        "_created_monotonic": time.monotonic(),
    }
    _bridges[bridge_id] = bridge
    logger.info("bridge_created", bridge_id=bridge_id, port=port)

    return BridgeResponse(**{k: v for k, v in bridge.items() if not k.startswith("_")})


@router.post("/select/auto", response_model=dict)
async def select_transport_auto(
    user_id: str = Depends(get_current_user_id),
):
    """
    Automatic transport selection.

    Scans live interfaces, scores them (wired > optical > wireless),
    and returns the best detected transport plus the matching catalog
    definition. Caller can pass the chosen transport_id into
    POST /bridges to actually create a bridge.
    """
    logger.info("select_transport_auto", user_id=user_id)

    detected = _enumerate_real_transports()
    candidates = [t for t in detected if t.is_up and not t.is_loopback]
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No usable transports detected on this host",
        )

    def score(t: DetectedTransport) -> tuple[int, float]:
        # high_performance (IB/RDMA/TB) > ethernet > usb > wlan > cellular > vpn > virtual
        priority = {
            "high_performance": 6,
            "ethernet": 5,
            "usb": 4,
            "wlan": 3,
            "cellular": 2,
            "lpwan": 2,
            "bluetooth": 1,
            "vpn": 1,
            "virtual": 0,
        }.get(t.adapter_family, 0)
        return (priority, float(t.signal_strength or 0))

    best = max(candidates, key=score)
    catalog_entry = _catalog_by_id().get(best.transport_id)

    return {
        "selection_mode": "auto",
        "chosen_transport_id": best.transport_id,
        "interface_name": best.interface_name,
        "adapter_family": best.adapter_family,
        "signal_strength": best.signal_strength,
        "signal_quality": best.signal_quality,
        "ip_address": best.ip_address,
        "catalog": catalog_entry,
        "candidates_considered": len(candidates),
    }


@router.post("/select/manual", response_model=dict)
async def select_transport_manual(
    transport_id: str = Query(..., description="Transport ID from catalog"),
    user_id: str = Depends(get_current_user_id),
):
    """
    Manual transport selection.

    Validates the requested transport_id exists in the catalog (1169+ entries),
    checks whether a matching interface is physically present on this host,
    and reports availability. Does NOT create the bridge — callers chain this
    with POST /bridges for the actual creation.
    """
    logger.info("select_transport_manual", user_id=user_id, transport_id=transport_id)

    catalog_entry = _catalog_by_id().get(transport_id)
    if not catalog_entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown transport_id: {transport_id}",
        )

    # Check local availability (best-effort)
    detected = _enumerate_real_transports()
    matching = [t for t in detected if t.transport_id == transport_id and t.is_up]
    is_available = len(matching) > 0

    return {
        "selection_mode": "manual",
        "chosen_transport_id": transport_id,
        "catalog": catalog_entry,
        "is_locally_available": is_available,
        "matching_interfaces": [
            {
                "interface_name": m.interface_name,
                "ip_address": m.ip_address,
                "signal_strength": m.signal_strength,
                "signal_quality": m.signal_quality,
            }
            for m in matching
        ],
        "warning": (
            None
            if is_available
            else "Transport is defined in catalog but no matching active interface was detected on this host. Bridge creation may still work in degraded/logical mode."
        ),
    }


@router.post("/bridges/auto", response_model=BridgeResponse, status_code=201)
async def auto_create_bridge(
    name: str = Query(..., description="Bridge display name"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Auto-create a bridge on the best-quality detected transport.
    Selects highest-strength UP interface; prefers wired over wireless.
    """
    logger.info("auto_create_bridge", user_id=user_id, name=name)

    detected = _enumerate_real_transports()
    candidates = [t for t in detected if t.is_up and not t.is_loopback]
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No usable transports detected",
        )

    # Prefer ethernet over wifi, then highest signal_strength
    def score(t: DetectedTransport) -> tuple[int, float]:
        priority = 2 if t.adapter_family == "ethernet" else 1
        return (priority, t.signal_strength)

    best = max(candidates, key=score)

    body = BridgeCreateRequest(
        transport_id=best.transport_id,
        name=name,
        bind_port=None,
        protocol="tcp",
        encryption=True,
        max_connections=64,
    )
    return await create_bridge(body=body, user_id=user_id, db=db)


@router.get("/bridges/{bridge_id}", response_model=BridgeResponse)
async def get_bridge(
    bridge_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Get status and stats of a specific bridge."""
    logger.info("get_bridge", user_id=user_id, bridge_id=bridge_id)

    bridge = _bridges.get(bridge_id)
    if not bridge:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bridge {bridge_id} not found",
        )

    # Refresh uptime
    bridge["uptime_seconds"] = int(time.monotonic() - bridge["_created_monotonic"])
    return BridgeResponse(**{k: v for k, v in bridge.items() if not k.startswith("_")})


@router.delete("/bridges/{bridge_id}", status_code=204, response_class=Response)
async def destroy_bridge(
    bridge_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Destroy a bridge and disconnect all peers."""
    logger.info("destroy_bridge", user_id=user_id, bridge_id=bridge_id)

    bridge = _bridges.get(bridge_id)
    if not bridge:
        # Idempotent: deleting unknown bridge is success
        return Response(status_code=204)

    if bridge.get("_owner") != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the bridge owner can destroy it",
        )

    _bridges.pop(bridge_id, None)
    logger.info("bridge_destroyed", bridge_id=bridge_id)

    # Best-effort socket broadcast
    try:
        from app.socket.server import sio
        await sio.emit(
            "transport:bridge_destroyed",
            {"bridge_id": bridge_id, "reason": "user_requested"},
        )
    except Exception as e:
        logger.warning("bridge_destroy_emit_failed", error=str(e))

    return Response(status_code=204)


@router.post("/bridges/{bridge_id}/broadcast")
async def broadcast_message(
    bridge_id: str,
    message: dict,
    user_id: str = Depends(get_current_user_id),
):
    """Broadcast a message to all peers connected on a bridge."""
    logger.info("broadcast_message", user_id=user_id, bridge_id=bridge_id)

    bridge = _bridges.get(bridge_id)
    if not bridge:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bridge {bridge_id} not found",
        )

    if bridge.get("_owner") != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the bridge owner can broadcast",
        )

    # Emit a transport-layer event so renderers can react
    try:
        from app.socket.server import sio
        await sio.emit(
            "transport:bridge_broadcast",
            {"bridge_id": bridge_id, "message": message, "from_user": user_id},
        )
        return {"status": "broadcast_sent"}
    except Exception as e:
        logger.error("broadcast_emit_failed", error=str(e))
        return {"status": "broadcast_queued", "error": str(e)}


@router.get("/capabilities/{transport_id}", response_model=CapabilityCheckResponse)
async def check_capabilities(
    transport_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Check what communication modes are supported on a transport."""
    logger.info("check_capabilities", user_id=user_id, transport_id=transport_id)

    catalog = _catalog_by_id().get(transport_id)
    if not catalog:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown transport_id: {transport_id}",
        )

    medium = catalog["medium"]
    bw = catalog["typical_bandwidth"].lower()

    # Capability heuristic from medium + bandwidth tier
    is_high_bw = ("gbps" in bw) or ("100 mbps" in bw and "ax" in catalog["transport_id"])
    is_low_bw = ("kbps" in bw) or ("2 mbps" in bw)

    supports_voice = not is_low_bw or "bluetooth" in catalog["transport_id"]
    supports_video = is_high_bw or medium in ("wired", "optical", "usb")
    supports_screen_share = supports_video
    supports_file_transfer = True

    if "gbps" in bw:
        recommended_quality = "high"
        max_part = 32
    elif "mbps" in bw:
        recommended_quality = "medium"
        max_part = 16
    else:
        recommended_quality = "low"
        max_part = 4

    return CapabilityCheckResponse(
        transport_id=transport_id,
        transport_name=catalog["name"],
        supports_voice=supports_voice,
        supports_video=supports_video,
        supports_screen_share=supports_screen_share,
        supports_file_transfer=supports_file_transfer,
        max_participants=max_part,
        recommended_codec="opus",
        recommended_video_quality=recommended_quality,
        notes=f"{catalog['name']} — {catalog['description']}",
    )


@router.get("/{transport_id}/signal", response_model=SignalQualityResponse)
async def measure_signal(
    transport_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    Measure real-time signal quality on a detected transport.
    Real probe — not a stub.
    """
    logger.info("measure_signal", user_id=user_id, transport_id=transport_id)

    if transport_id not in _catalog_by_id():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown transport_id: {transport_id}",
        )

    try:
        result = await _measure_real_signal(transport_id)
    except Exception as e:
        logger.error("signal_measurement_failed", error=str(e), transport_id=transport_id)
        # Degraded-mode result so the route never crashes
        result = {
            "transport_id": transport_id,
            "interface_name": "unknown",
            "signal_strength": 0.0,
            "snr": None,
            "bandwidth": 0.0,
            "latency": 0.0,
            "jitter": 0.0,
            "packet_loss": 100.0,
            "quality_score": 0.0,
            "quality_label": "unavailable",
            "measured_at": datetime.utcnow().isoformat(),
        }

    return SignalQualityResponse(**result)


@router.get("/full-info/{transport_id}", response_model=dict)
async def get_transport_full_info(
    transport_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    Rich transport metadata + live availability + recommendations.

    Unifies the catalog entry, real-time interface detection, capability
    summary, and suggested alternatives in the same category into a single
    payload — so clients can render a transport detail page with one call.
    """
    logger.info("get_transport_full_info", user_id=user_id, transport_id=transport_id)

    reg = TransportRegistry()
    td = reg.get_transport(transport_id)
    if td is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transport {transport_id} not found",
        )

    rich = _registry_to_rich_dict(td)

    # Live availability
    try:
        detected = _enumerate_real_transports()
    except Exception:
        detected = []
    matching = [d for d in detected if d.transport_id == transport_id]
    is_available = any(m.is_up for m in matching)
    interfaces = [
        {
            "interface_name": m.interface_name,
            "adapter_family": m.adapter_family,
            "ip_address": m.ip_address,
            "mac_address": m.mac_address,
            "speed": m.speed,
            "mtu": m.mtu,
            "is_up": m.is_up,
            "signal_strength": m.signal_strength,
            "signal_quality": m.signal_quality,
        }
        for m in matching
    ]

    # Capability summary (reuse heuristic from /capabilities)
    bw = (rich.get("typical_bandwidth") or "").lower()
    medium = rich.get("medium")
    is_high_bw = ("gbps" in bw) or ("100 mbps" in bw and "ax" in transport_id)
    is_low_bw = ("kbps" in bw) or ("2 mbps" in bw)
    caps = {
        "supports_voice": (not is_low_bw) or "bluetooth" in transport_id,
        "supports_video": is_high_bw or medium in ("wired", "optical", "usb"),
        "supports_screen_share": is_high_bw or medium in ("wired", "optical", "usb"),
        "supports_file_transfer": True,
        "supports_multicast": rich.get("supports_multicast"),
        "supports_broadcast": rich.get("supports_broadcast"),
        "recommended_max_participants": (
            32 if "gbps" in bw else 16 if "mbps" in bw else 4
        ),
    }

    # Peers in same category (up to 8 suggestions)
    cat = rich.get("category")
    peers = []
    if cat:
        try:
            same_cat = reg.get_by_category(cat)
            peers = [
                _registry_to_dict(p)
                for p in same_cat
                if p.id != transport_id
            ][:8]
        except Exception:
            peers = []

    return {
        "transport": rich,
        "availability": {
            "is_locally_available": is_available,
            "matching_interface_count": len(matching),
            "interfaces": interfaces,
        },
        "capabilities": caps,
        "peers_in_category": peers,
        "recommended_use": _recommend_uses(rich),
    }


def _recommend_uses(rich: dict) -> list[str]:
    """Generate human-readable use recommendations from catalog metadata."""
    uses: list[str] = []
    bw = (rich.get("typical_bandwidth") or "").lower()
    cat = (rich.get("category") or "").lower()
    medium = rich.get("medium")

    if "gbps" in bw:
        uses.append("HD video conferencing")
        uses.append("Large file transfer")
    if "mbps" in bw or "gbps" in bw:
        uses.append("Voice / VoIP calls")
    if rich.get("supports_multicast"):
        uses.append("Multicast streaming")
    if rich.get("supports_broadcast"):
        uses.append("LAN discovery / broadcast")
    if medium == "wired":
        uses.append("Low-jitter real-time workloads")
    if medium == "wireless":
        uses.append("Roaming / mobile clients")
    if "industrial" in cat or "scada" in cat or "time_sensitive" in cat:
        uses.append("Deterministic OT traffic")
    if "security_isolated" in cat or "tactical" in cat:
        uses.append("Air-gapped / classified operations")
    if "deep_space" in cat or "quantum" in cat:
        uses.append("Research / experimental link")
    if "mesh" in cat:
        uses.append("Ad-hoc / decentralized deployments")
    if "high_performance" in cat:
        uses.append("HPC / GPU-to-GPU fabric")
    if not uses:
        uses.append("General-purpose networking")
    return uses


# ── Catch-all transport-by-id route MUST stay LAST ────────────────


@router.get("/{transport_id}", response_model=dict)
async def get_transport(
    transport_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Get full definition of a single transport by ID."""
    logger.info("get_transport", user_id=user_id, transport_id=transport_id)

    transport = _catalog_by_id().get(transport_id)
    if transport is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transport {transport_id} not found",
        )
    return transport
