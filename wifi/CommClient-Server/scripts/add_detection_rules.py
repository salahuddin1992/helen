"""Add detection rules for the new transport categories introduced by
the 1169-entry catalog.  Idempotent — existing keys are left untouched."""
from __future__ import annotations

import json
from pathlib import Path

RULES_PATH = (
    Path(__file__).resolve().parent.parent
    / "app"
    / "transports"
    / "config"
    / "detection_rules.json"
)

NEW_RULES: dict[str, dict] = {
    "security_isolated": {
        "methods": ["interface_scan", "policy_check", "isolation_probe"],
        "windows_commands": [
            "Get-NetFirewallProfile",
            "Get-NetAdapter | Where-Object {$_.Name -like '*Secure*' -or $_.Name -like '*Isolated*'}",
        ],
        "linux_commands": [
            "ip netns list",
            "iptables -L -n",
            "firewall-cmd --list-all",
        ],
        "interface_patterns": ["secure*", "isolated*", "crypto*", "dmz*", "enclave*", "airgap*"],
        "isolation_indicators": ["no_default_route", "namespaced", "policy_locked"],
        "notes": "Air-gapped / zero-trust / microsegmented fabrics — detected by policy and naming",
    },
    "military_defense": {
        "methods": ["hardware_probe", "waveform_detect", "policy_check"],
        "interface_patterns": ["sipr*", "nipr*", "jtrs*", "link16*", "link22*", "mads*", "haipe*"],
        "waveforms": ["Link-16", "Link-22", "SRW", "WNW", "MUOS", "SINCGARS"],
        "notes": "Tactical/classified radios — detection relies on named adapters or crypto modules",
    },
    "broadcast_media": {
        "methods": ["interface_scan", "service_discovery"],
        "interface_patterns": ["smpte*", "st2110*", "ndi*", "dante*", "aes67*", "livu*"],
        "service_ports": [5004, 5353, 6000, 319, 320],
        "notes": "Studio / live production networks (SMPTE 2110, NDI, Dante, AES67)",
    },
    "deep_space": {
        "methods": ["hardware_probe", "telemetry_check"],
        "interface_patterns": ["ccsds*", "dtnet*", "deepspace*", "prox*"],
        "protocols": ["CCSDS", "SPP", "Proximity-1", "AOS", "DTN", "LTP"],
        "notes": "Deep-space / inter-satellite links — typically logical, require vendor SDRs",
    },
    "cellular_private": {
        "methods": ["interface_scan", "hardware_probe"],
        "interface_patterns": [
            "cbrs*",
            "private5g*",
            "ran*",
            "oran*",
            "cpri*",
            "ecpri*",
            "fronthaul*",
            "midhaul*",
            "backhaul*",
        ],
        "notes": "Private LTE/5G, CBRS, O-RAN fronthaul/midhaul/backhaul",
    },
    "maritime_underwater": {
        "methods": ["hardware_probe"],
        "interface_patterns": ["marine*", "nmea*", "ship*", "submarine*", "buoy*", "hydrophone*"],
        "protocols": ["NMEA 2000", "NMEA 0183", "UWAN", "JANUS"],
        "notes": "Shipboard, submarine, buoy, hydrophone links",
    },
    "mining_underground": {
        "methods": ["hardware_probe"],
        "interface_patterns": ["mine*", "borehole*", "seismic*", "underground*"],
        "notes": "Mine / borehole / underground comms — often leaky-feeder or TTE",
    },
    "railway": {
        "methods": ["hardware_probe"],
        "interface_patterns": ["rail*", "train*", "cbtc*", "etcs*", "ptc*"],
        "protocols": ["CBTC", "ETCS", "PTC", "TCN"],
        "notes": "Train control and wayside signaling networks",
    },
    "transport_vehicle": {
        "methods": ["hardware_probe", "interface_scan"],
        "interface_patterns": ["v2x*", "can*", "canbus*", "obd*", "ev*", "chademo*", "ccs*", "ocpp*"],
        "protocols": ["V2X", "DSRC", "C-V2X", "OCPP", "ISO 15118"],
        "notes": "Vehicular / EV charging / fleet telematics",
    },
    "financial_trading": {
        "methods": ["service_discovery", "latency_probe"],
        "service_ports": [4001, 9001, 9000, 443, 465, 2195],
        "protocols": ["FIX", "ITCH", "OUCH", "FAST", "SWIFT"],
        "notes": "Low-latency trading — detected by protocol fingerprint, not adapter",
    },
    "medical": {
        "methods": ["service_discovery"],
        "service_ports": [104, 2761, 2762, 11112, 2575],
        "protocols": ["HL7", "DICOM", "IHE"],
        "notes": "Hospital / PACS / DICOM / HL7 networks",
    },
    "energy_grid": {
        "methods": ["service_discovery", "hardware_probe"],
        "service_ports": [102, 502, 20000, 44818],
        "protocols": [
            "IEC 61850",
            "DNP3",
            "Modbus TCP",
            "IEC 60870-5-104",
            "IEEE C37.118",
        ],
        "interface_patterns": ["substation*", "pmu*", "scada*", "rtu*"],
        "notes": "Smart grid / substation automation / PMU synchrophasors",
    },
    "quantum_experimental": {
        "methods": ["hardware_probe"],
        "interface_patterns": ["qkd*", "qnet*", "quantum*", "cryo*"],
        "notes": "Quantum key distribution and experimental research testbeds",
    },
    "drone_uav": {
        "methods": ["hardware_probe", "radio_scan"],
        "interface_patterns": ["uav*", "uas*", "drone*", "mavlink*"],
        "protocols": ["MAVLink", "UAVCAN", "DroneID", "RemoteID"],
        "service_ports": [14550, 14551],
        "notes": "UAV command-and-control and telemetry",
    },
    "iot_sensor": {
        "methods": ["radio_scan", "service_discovery"],
        "protocols": ["CoAP", "MQTT", "MQTT-SN", "Matter", "Thread", "Zigbee"],
        "service_ports": [5683, 1883, 8883, 5540],
        "interface_patterns": ["zigbee*", "thread*", "matter*", "xbee*", "6lowpan*"],
        "notes": "Personal/body-area, wireless sensor networks, RTLS",
    },
    "storage_network": {
        "methods": ["service_discovery", "hardware_probe"],
        "service_ports": [3260, 860, 4420],
        "protocols": ["iSCSI", "NVMe-oF", "FCoE", "FC"],
        "interface_patterns": ["san*", "fc*", "fcoe*", "nvme*"],
        "notes": "Storage area networks and NVMe over fabric",
    },
    "wan_private": {
        "methods": ["interface_scan", "route_probe"],
        "interface_patterns": ["mpls*", "sonet*", "sdh*", "otn*", "mlpp*", "wan*"],
        "protocols": ["MPLS", "SONET", "SDH", "OTN", "Segment Routing"],
        "notes": "Private WAN / metro transport / leased lines",
    },
    "building_campus": {
        "methods": ["hardware_probe", "service_discovery"],
        "protocols": ["BACnet", "KNX", "LonWorks", "Modbus", "ONVIF"],
        "service_ports": [47808, 3671, 1628, 3702, 80, 554],
        "interface_patterns": ["bacnet*", "knx*", "lon*", "cctv*", "onvif*"],
        "notes": "Building automation, access control, CCTV",
    },
    "datacenter_fabric": {
        "methods": ["hardware_probe", "ecmp_probe"],
        "interface_patterns": ["spine*", "leaf*", "tor*", "fabric*"],
        "protocols": ["BGP EVPN", "VXLAN", "Clos/Spine-Leaf"],
        "notes": "Datacenter spine/leaf/ToR disaggregated fabrics",
    },
    "scada_utility": {
        "methods": ["service_discovery", "hardware_probe"],
        "protocols": ["Modbus", "DNP3", "IEC 61850", "IEC 60870-5-104"],
        "service_ports": [502, 20000, 102, 2404],
        "interface_patterns": ["plc*", "rtu*", "ied*", "scada*"],
        "notes": "Utility SCADA, PLC, RTU, IED telemetry",
    },
    "tactical_emergency": {
        "methods": ["radio_scan", "hardware_probe"],
        "protocols": ["RoIP", "P25", "TETRA", "DMR"],
        "interface_patterns": ["roip*", "p25*", "tetra*", "dmr*", "firstnet*"],
        "notes": "Public safety / first-responder / tactical dispatch",
    },
    "specialty_vertical": {
        "methods": ["service_discovery"],
        "notes": "Cross-vertical specialty nets — detection is per-protocol, not per-adapter",
    },
    "overlay_tunnel": {
        "methods": ["interface_scan", "tunnel_probe"],
        "interface_patterns": ["vxlan*", "geneve*", "gre*", "ipip*", "l2tp*", "evpn*"],
        "protocols": ["VXLAN", "Geneve", "GRE", "L2TP", "EVPN", "SR-MPLS", "SRv6"],
        "notes": "Overlay / SDN tunnel fabrics",
    },
    "high_performance": {
        "methods": ["hardware_probe"],
        "interface_patterns": ["ib*", "mlx*", "roce*", "rdma*", "nvlink*", "thunderbolt*", "tb*"],
        "protocols": ["InfiniBand", "RoCE", "iWARP", "Omni-Path", "NVLink", "CXL", "Gen-Z"],
        "notes": "HPC / GPU fabric / coherent memory interconnects",
    },
    "vpn": {
        "methods": ["interface_scan", "tunnel_probe"],
        "interface_patterns": ["tun*", "tap*", "wg*", "ipsec*", "tailscale*", "zerotier*", "zt*"],
        "protocols": ["WireGuard", "OpenVPN", "IPsec/IKEv2", "Tailscale", "ZeroTier"],
        "notes": "Encrypted virtual tunnels — detected by kernel interface",
    },
    "virtual": {
        "methods": ["interface_scan"],
        "interface_patterns": [
            "docker*",
            "br-*",
            "veth*",
            "virbr*",
            "vmnet*",
            "vethernet*",
            "hyperv*",
            "wsl*",
            "vbox*",
        ],
        "notes": "Virtual switches / container bridges / hypervisor adapters",
    },
}


def main() -> None:
    data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    before = len(data)
    added = 0
    for key, rule in NEW_RULES.items():
        if key not in data:
            data[key] = rule
            added += 1
    RULES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"detection_rules.json: +{added} (from {before} to {len(data)})")


if __name__ == "__main__":
    main()
