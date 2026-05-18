"""Append missing transport types to catalog (one-shot script)."""
from __future__ import annotations
import json
from pathlib import Path

CATALOG = Path(__file__).parent.parent / "app" / "transports" / "config" / "transport_catalog.json"


def T(**kw: object) -> dict:
    defaults = {
        "subcategory": None, "max_nodes": None,
        "supports_multicast": True, "supports_broadcast": True,
        "duplex": "full", "requires_hardware": True,
        "is_common": False, "detection_method": "hardware_probe",
    }
    return {**defaults, **kw}


NEW = [
    T(id="wifi_7", name="Wi-Fi 7 (802.11be)", category="wifi",
      description="Extremely High Throughput WLAN with 320MHz channels, 4K-QAM, MLO.",
      layer=2, medium="wireless", typical_bandwidth="46 Gbps", typical_range="150m",
      latency_class="ultra_low", detection_method="driver_check",
      adapter_family="wifi7", security_level="high"),
    T(id="wifi_7_mlo", name="Wi-Fi 7 Multi-Link Operation (MLO)", category="wifi",
      description="Simultaneous 2.4/5/6 GHz operation under 802.11be.",
      layer=2, medium="wireless", typical_bandwidth="46 Gbps", typical_range="150m",
      latency_class="ultra_low", detection_method="driver_check",
      adapter_family="wifi7", security_level="high"),
    T(id="nr_sidelink", name="5G NR Sidelink (D2D)", category="cellular_private",
      description="Direct device-to-device 5G NR communication without base station.",
      layer=2, medium="wireless", typical_bandwidth="1 Gbps", typical_range="1000m",
      latency_class="ultra_low", detection_method="driver_check",
      adapter_family="5g", security_level="high"),
    T(id="fddi", name="FDDI (Fiber Distributed Data Interface)", category="legacy",
      description="Legacy 100 Mbps dual-ring token-passing fiber network.",
      layer=2, medium="optical", typical_bandwidth="100 Mbps", typical_range="200km",
      latency_class="low", detection_method="interface_scan",
      adapter_family="fddi", security_level="basic", max_nodes=500),
    T(id="arcnet", name="ARCNET (ANSI 878.1)", category="legacy",
      description="Legacy token-passing industrial LAN at 2.5/20 Mbps.",
      layer=2, medium="wired", typical_bandwidth="2.5 Mbps", typical_range="600m",
      latency_class="medium", detection_method="interface_scan",
      adapter_family="arcnet", security_level="none", max_nodes=255),
    T(id="appletalk", name="AppleTalk (LocalTalk)", category="legacy",
      description="Legacy Apple networking stack; discontinued since Mac OS X 10.6.",
      layer=3, medium="wired", typical_bandwidth="230.4 kbps", typical_range="300m",
      latency_class="medium", detection_method="interface_scan",
      adapter_family="localtalk", security_level="none"),
    T(id="ipx_spx", name="IPX/SPX (Novell NetWare)", category="legacy",
      description="Legacy Novell internetwork packet exchange protocol suite.",
      layer=3, medium="wired", typical_bandwidth="10 Mbps", typical_range="100m",
      latency_class="medium", detection_method="interface_scan",
      adapter_family="ethernet", requires_hardware=False, security_level="basic"),
    T(id="ptp_1588", name="PTP (IEEE 1588 Precision Time Protocol)",
      category="time_sensitive",
      description="Sub-microsecond clock synchronization for TSN/audio/industrial.",
      layer=2, medium="wired", typical_bandwidth="100 Mbps+", typical_range="100m",
      latency_class="ultra_low", detection_method="service_discovery",
      adapter_family="ethernet", requires_hardware=False, security_level="basic"),
    T(id="sync_ethernet", name="Synchronous Ethernet (SyncE, ITU-T G.8261)",
      category="time_sensitive",
      description="Physical-layer frequency sync over Ethernet; telecom carrier grade.",
      layer=1, medium="wired", typical_bandwidth="1-400 Gbps", typical_range="100m",
      latency_class="ultra_low", detection_method="driver_check",
      adapter_family="ethernet", security_level="basic"),
    T(id="matter", name="Matter (CSA, IPv6 over Thread/Wi-Fi)",
      category="iot_sensor",
      description="Unified smart-home IoT standard over Thread/Wi-Fi with IPv6.",
      layer=7, medium="wireless", typical_bandwidth="250 kbps", typical_range="30m",
      latency_class="low", detection_method="service_discovery",
      adapter_family="thread", requires_hardware=False, is_common=True,
      security_level="high"),
    T(id="sigfox", name="Sigfox (UNB LPWAN)", category="iot_sensor",
      description="Ultra-narrowband 0G LPWAN for low-power IoT telemetry.",
      layer=2, medium="wireless", typical_bandwidth="100 bps", typical_range="50km",
      latency_class="very_high", detection_method="driver_check",
      adapter_family="sigfox", security_level="basic"),
    T(id="enocean", name="EnOcean (ISO/IEC 14543-3-10)", category="iot_sensor",
      description="Energy-harvesting wireless for battery-less building automation.",
      layer=2, medium="wireless", typical_bandwidth="125 kbps", typical_range="300m",
      latency_class="low", detection_method="hardware_probe",
      adapter_family="enocean", security_level="basic"),
    T(id="coap", name="CoAP (RFC 7252)", category="iot_sensor",
      description="Constrained Application Protocol over UDP for resource-limited IoT.",
      layer=7, medium="virtual", typical_bandwidth="varies", typical_range="Internet",
      latency_class="low", detection_method="port_scan",
      adapter_family="ethernet", requires_hardware=False, security_level="medium"),
    T(id="cxl", name="CXL (Compute Express Link)", category="high_performance",
      description="Cache-coherent interconnect over PCIe for CPU/GPU/memory pooling.",
      layer=1, medium="wired", typical_bandwidth="256 GB/s", typical_range="0.5m",
      latency_class="ultra_low", detection_method="hardware_probe",
      adapter_family="pcie", security_level="high"),
    T(id="gen_z", name="Gen-Z Fabric", category="high_performance",
      description="Memory-semantic open fabric interconnect (merged into CXL).",
      layer=1, medium="wired", typical_bandwidth="400 Gbps", typical_range="2m",
      latency_class="ultra_low", detection_method="hardware_probe",
      adapter_family="genz", security_level="high"),
    T(id="ccix", name="CCIX (Cache Coherent Interconnect)",
      category="high_performance",
      description="Accelerator-to-CPU cache coherent interconnect over PCIe.",
      layer=1, medium="wired", typical_bandwidth="25 GT/s", typical_range="0.3m",
      latency_class="ultra_low", detection_method="hardware_probe",
      adapter_family="pcie", security_level="high"),
    T(id="lvds", name="LVDS (ANSI/TIA/EIA-644)", category="serial_bus",
      description="Low-voltage differential signaling for high-speed backplanes.",
      layer=1, medium="wired", typical_bandwidth="3.125 Gbps", typical_range="10m",
      latency_class="ultra_low", detection_method="hardware_probe",
      adapter_family="lvds", security_level="none"),
    T(id="gpib", name="GPIB (IEEE-488)", category="serial_bus",
      description="General Purpose Instrumentation Bus for laboratory equipment.",
      layer=1, medium="wired", typical_bandwidth="8 MB/s", typical_range="20m",
      latency_class="medium", detection_method="hardware_probe",
      adapter_family="gpib", security_level="none", max_nodes=15),
    T(id="dmx512", name="DMX512 (ANSI E1.11)", category="av_network",
      description="Stage lighting/pyrotechnics control over RS-485.",
      layer=1, medium="wired", typical_bandwidth="250 kbps", typical_range="1000m",
      latency_class="low", detection_method="hardware_probe",
      adapter_family="rs485", security_level="none", max_nodes=512),
    T(id="lifi_ieee", name="Li-Fi (IEEE 802.11bb)", category="optical_link",
      description="Visible light communication per IEEE 802.11bb standard.",
      layer=1, medium="optical", typical_bandwidth="224 Gbps", typical_range="10m",
      latency_class="ultra_low", detection_method="hardware_probe",
      adapter_family="lifi", security_level="high"),
    T(id="10gbe", name="10 Gigabit Ethernet (10GBASE-*)", category="ethernet",
      description="IEEE 802.3ae — 10 Gbps over copper and fiber.",
      layer=2, medium="wired", typical_bandwidth="10 Gbps", typical_range="100m",
      latency_class="ultra_low", detection_method="interface_scan",
      adapter_family="ethernet", is_common=True, security_level="basic"),
    T(id="usb4", name="USB4 (40/80 Gbps)", category="serial_bus",
      description="USB4 with DisplayPort/PCIe tunneling and Thunderbolt compat.",
      layer=1, medium="wired", typical_bandwidth="80 Gbps", typical_range="0.8m",
      latency_class="ultra_low", detection_method="hardware_probe",
      adapter_family="usb", security_level="basic"),
    T(id="usb_ethernet", name="USB Ethernet Adapter", category="ethernet",
      description="Ethernet-over-USB adapters and tethering (CDC/NCM/RNDIS).",
      layer=2, medium="wired", typical_bandwidth="1 Gbps", typical_range="5m",
      latency_class="ultra_low", detection_method="hardware_probe",
      adapter_family="usb", is_common=True, security_level="basic"),
    T(id="vlan", name="IEEE 802.1Q VLAN", category="topology",
      description="Virtual LAN tagging for broadcast domain segmentation.",
      layer=2, medium="virtual", typical_bandwidth="varies", typical_range="LAN",
      latency_class="ultra_low", detection_method="interface_scan",
      adapter_family="ethernet", requires_hardware=False, security_level="basic"),
    T(id="intranet", name="Intranet (Private IP)", category="wan_private",
      description="Private IP network scoped within an organization.",
      layer=3, medium="virtual", typical_bandwidth="varies", typical_range="organization",
      latency_class="low", detection_method="service_discovery",
      adapter_family="ip", requires_hardware=False, is_common=True,
      security_level="medium"),
    T(id="adhoc_wireless", name="Ad Hoc Wireless (IBSS)", category="mesh",
      description="Peer-to-peer Wi-Fi Independent Basic Service Set.",
      layer=2, medium="wireless", typical_bandwidth="54 Mbps", typical_range="50m",
      latency_class="low", detection_method="driver_check",
      adapter_family="wifi", requires_hardware=False, security_level="basic"),
    T(id="mqtt_sn", name="MQTT-SN (Sensor Networks)", category="iot_sensor",
      description="MQTT variant for UDP and resource-constrained sensor networks.",
      layer=7, medium="virtual", typical_bandwidth="varies", typical_range="LAN",
      latency_class="low", detection_method="port_scan",
      adapter_family="ethernet", requires_hardware=False, security_level="medium"),
    T(id="nvlink", name="NVIDIA NVLink", category="high_performance",
      description="High-bandwidth GPU-to-GPU and GPU-to-CPU interconnect.",
      layer=1, medium="wired", typical_bandwidth="900 GB/s", typical_range="0.3m",
      latency_class="ultra_low", detection_method="hardware_probe",
      adapter_family="gpu", security_level="high"),
]


def main() -> None:
    data = json.load(CATALOG.open("r", encoding="utf-8"))
    is_list = isinstance(data, list)
    entries = data if is_list else data.get("transports", [])
    existing = {e["id"] for e in entries}

    to_add = [t for t in NEW if t["id"] not in existing]
    skipped = len(NEW) - len(to_add)

    entries.extend(to_add)
    if is_list:
        data = entries
    else:
        data["transports"] = entries

    with CATALOG.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Added: {len(to_add)}")
    print(f"Skipped (already present): {skipped}")
    print(f"Final catalog size: {len(entries)}")


if __name__ == "__main__":
    main()
