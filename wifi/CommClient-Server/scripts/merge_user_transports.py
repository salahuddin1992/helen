"""Merge the user-supplied 759-item transport list into transport_catalog.json.

Strategy:
- Normalize names (strip, lower, collapse whitespace) to detect duplicates.
- For items already present (same normalized name, alias, or id), skip.
- For new items, auto-assign category/medium/latency using keyword heuristics.
- Build stable IDs by slugifying the English portion.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CATALOG = ROOT / "app" / "transports" / "config" / "transport_catalog.json"
USER_LIST = HERE / "user_transport_list.txt"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    s = re.sub(r"[^a-z0-9\u0600-\u06ff ]", "", s)
    return s


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    # strip arabic & non-ascii
    s = "".join(c for c in s if ord(c) < 128)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s or "transport"


CATEGORY_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(wi-?fi|802\.11|mesh wi-?fi|wlan)\b", re.I), "wifi"),
    (re.compile(r"\b(fiber|fibre|pon|gpon|epon|cwdm|dwdm|optical ring|dark fiber|rf over fiber)\b", re.I), "fiber"),
    (re.compile(r"\b(wireless bridge|point-to-point|point-to-multipoint)\b", re.I), "wireless_bridge"),
    (re.compile(r"\b(private lte|private 5g|cbrs|nb-iot|lte-m|femto|pico|macro|small cell|cloud-ran|open ran|o-ran|v?ran)\b", re.I), "cellular_private"),
    (re.compile(r"\b(dmr|tetra|p25|hf|vhf|uhf|packet radio|troposcatter|manpack|sdr|satcom|rf over|analog optical|radio relay)\b", re.I), "radio"),
    (re.compile(r"\b(batman|babel|cjdns|manet|vanet|mesh|community mesh|swarm|offline mesh)\b", re.I), "mesh"),
    (re.compile(r"\b(profinet|profibus|ethercat|ethernet/?ip|modbus|devicenet|controlnet|sercos|cc-link|interbus|foundation fieldbus|industrial ethernet|powerlink|varan|fl-net|mechatrolink|as-interface|io-link|goose|sampled values|iec 61850)\b", re.I), "industrial"),
    (re.compile(r"\b(rs-485|rs-232|serial|can bus|spi|i2c|i3c|1-wire|gpib|lvds|lin|firewire|thunderbolt|usb4)\b", re.I), "serial_bus"),
    (re.compile(r"\b(powerline|homeplug|g\.hn|bpl|ethernet-apl|podl)\b", re.I), "powerline"),
    (re.compile(r"\b(free-space optical|laser bridge|infrared link|visible light|vlc|li-?fi|optical link|laser)\b", re.I), "optical_link"),
    (re.compile(r"\b(infiniband|omni-?path|nvlink|pcie|rapidio|hypertransport|myrinet|sci|hippi|cxl|gen-z|ccix|tpu|gpu fabric|rdma|roce|nvme|render farm|hpc|ai training|ai inference|accelerator fabric|memory fabric|network-on-chip|chiplet)\b", re.I), "high_performance"),
    (re.compile(r"\b(mpls|sd-wan|vpls|evpn|vxlan|geneve|gre|ipsec|wireguard|l2tp|pppoe|segment routing|trill|spb|fabricpath|stackwise|virtual chassis)\b", re.I), "overlay_tunnel"),
    (re.compile(r"\b(dante|aes67|ravenna|q-lan|madi|hdbaset|sdi|ndi|smpte|sdvoe|ethersound|cobranet|avb|audio video bridging|dmx)\b", re.I), "av_network"),
    (re.compile(r"\b(wireless sensor|ble beacon|thread|6lowpan|zigbee|xbee|ant|nfc|sigfox|enocean|matter|coap|mqtt|rtls|uwb|asset tracking)\b", re.I), "iot_sensor"),
    (re.compile(r"\b(fddi|token ring|arcnet|atm lan|isdn|frame relay|x\.25|t1|e1|ds3|appletalk|ipx)\b", re.I), "legacy"),
    (re.compile(r"\b(fcoe|iscsi|san|storage|nvme over|fibre channel)\b", re.I), "storage_network"),
    (re.compile(r"\b(ipmi|bmc|redfish|baseboard|chassis management|oob|out-of-band|kvm over ip|serial console|cwmp)\b", re.I), "management"),
    (re.compile(r"\b(satellite|cubesat|haps|balloon|near-space|inter-satellite|space)\b", re.I), "satellite_aerospace"),
    (re.compile(r"\b(tactical|emergency|dispatch radio|roip|first responder|fire station|police|ambulance|disaster|eoc|mutual aid)\b", re.I), "tactical_emergency"),
    (re.compile(r"\b(topology|spine-?leaf|clos|fat-?tree|dragonfly|torus|hypercube|ring topology|bus topology|star topology|tree topology|mesh network|daisy|hub-and-spoke|leaf-spine|super spine|non-blocking|any-to-any|rearrangement)\b", re.I), "topology"),
    (re.compile(r"\b(datacenter|spine|leaf|top-of-rack|end-of-row|middle-of-row|rack interconnect|disaggregated|composable)\b", re.I), "datacenter_fabric"),
    (re.compile(r"\b(air-?gap|closed loop|quarantine|bastion|jump host|secure enclave|classified|cross-domain|data diode|one-way|guest isolation|byod|dmz|zero-?trust|crypto-isolated|honeynet|deception|red team|blue team|malware detonation|sandbox|isolated)\b", re.I), "security_isolated"),
    (re.compile(r"\b(building|campus|elevator|access control|cctv|intercom|dorm|library|school|classroom|hotel|resort|cruise|theme park|museum|casino|stadium|venue|smart building|recreation)\b", re.I), "building_campus"),
    (re.compile(r"\b(scada|plc|rtu|ied|protection relay|synchrophasor|pmu|metering|ami|substation|utility|distribution automation|pipeline|wellhead)\b", re.I), "scada_utility"),
    (re.compile(r"\b(vehicular|v2x|roadside|ev charging|charging depot|fleet telematics|taxi|bus depot|traffic signal|tolling|parking)\b", re.I), "transport_vehicle"),
    (re.compile(r"\b(service mesh|message bus|event bus|pub/sub|message queue|stream processing|command bus|request/reply|brokerless|ddss|tipc|kubernetes|cni|cluster|consensus)\b", re.I), "service_overlay"),
    (re.compile(r"\b(sd-wan|leased line|metro ethernet|carrier ethernet|intranet|wan private|dark fiber ring|metro dark fiber)\b", re.I), "wan_private"),
    (re.compile(r"\b(time-sensitive|tsn|ptp|sync|ntp|white rabbit|deterministic)\b", re.I), "time_sensitive"),
    (re.compile(r"\b(military|battlefield|command post|tactical operations|isr|border surveillance|coastal|manpack|classified compartmented)\b", re.I), "military_defense"),
    (re.compile(r"\b(automotive|lin|flexray|most|broadr|v2x|ev charging|automotive assembly|battery plant|fleet)\b", re.I), "automotive"),
    (re.compile(r"\b(medical|hospital|nurse call|pacs|dicom|pharmacy|laboratory|icu|ambulance telemetry)\b", re.I), "medical"),
    (re.compile(r"\b(maritime|shipboard|submarine|harbor|marine buoy|oceanographic|aquaculture|port terminal|container yard|coastal)\b", re.I), "maritime_underwater"),
    (re.compile(r"\b(energy grid|smart grid|microgrid|bess|solar farm|wind farm|power plant|substation automation|dam control)\b", re.I), "energy_grid"),
    (re.compile(r"\b(deep space|inter-satellite|cubesat|haps|balloon|near-space|space)\b", re.I), "deep_space"),
    (re.compile(r"\b(quantum|cryogenic|particle lab|telescope|observatory|research instrument)\b", re.I), "quantum_experimental"),
    (re.compile(r"\b(aircraft|aviation|airport|baggage|apron|cabin|arinc|afdx|avionics)\b", re.I), "aviation"),
    (re.compile(r"\b(mining|mine|borehole|seismic|underground)\b", re.I), "mining_underground"),
    (re.compile(r"\b(rail|train|cbtc|etcs|positive train|rail signaling)\b", re.I), "railway"),
    (re.compile(r"\b(broadcast|studio|live event|newsroom|color grading|foley|post-production|motion capture|ar collaboration|vr free-roam|digital signage|iptv|multicast distribution)\b", re.I), "broadcast_media"),
    (re.compile(r"\b(trading|market data|settlement|clearing|atm banking|core banking|pos payment|kiosk|branch interconnect|retail edge|franchise|cold chain)\b", re.I), "financial_trading"),
    (re.compile(r"\b(nuclear)\b", re.I), "nuclear"),
    (re.compile(r"\b(public safety|lte|first responder|fire station|police dispatch|ambulance|emergency operations|mutual aid)\b", re.I), "emergency_public_safety"),
    (re.compile(r"\b(acoustic|underwater)\b", re.I), "acoustic"),
    (re.compile(r"\b(drone|uav|uas|swarm edge)\b", re.I), "drone_uav"),
    (re.compile(r"\b(ethernet|10base|100base|1000base|10gbase|25gbe|40gbe|100gbe|200gbe|400gbe|800gbe|single pair|coaxial ethernet|pseudowire|ethernet over|lossless ethernet)\b", re.I), "ethernet"),
    (re.compile(r"\b(vlan|intranet|overlay network|underlay network|peer-to-peer local|client-server local|hybrid local|offline collaboration|sneakernet|store-and-forward|delay-tolerant|opportunistic|temporary field lan|rapid deployment|expeditionary|harsh environment|ruggedized|edge cache|local cdn|patch mirror|repository mirror|package cache|local registry)\b", re.I), "specialty_vertical"),
]

MEDIUM_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(fiber|fibre|optical|laser|gpon|epon|cwdm|dwdm|vlc|li-?fi|visible light)\b", re.I), "optical"),
    (re.compile(r"\b(wireless|wi-?fi|radio|microwave|millimeter|satellite|cellular|5g|lte|nr|cbrs|lora|zigbee|ble|bluetooth|nfc|uwb|drone|mesh wireless|infrared|troposcatter|hf|vhf|uhf|manet|vanet)\b", re.I), "wireless"),
    (re.compile(r"\b(vlan|overlay|underlay|tunnel|virtual|service mesh|event bus|message bus|mpls|evpn|vxlan|geneve|gre|ipsec|wireguard|l2tp|pppoe|sd-wan|segment routing|trill|spb|intranet|extranet|dmz|zero-trust|pseudowire|network-on-chip)\b", re.I), "virtual"),
    (re.compile(r"\b(hybrid|packet-optical)\b", re.I), "hybrid"),
]


def pick_category(name: str) -> str:
    for pat, cat in CATEGORY_HINTS:
        if pat.search(name):
            return cat
    return "custom"


def pick_medium(name: str) -> str:
    for pat, med in MEDIUM_HINTS:
        if pat.search(name):
            return med
    return "wired"


def pick_latency(name: str, medium: str) -> str:
    if re.search(r"\b(trading|ultra|tsn|deterministic|ptp|sync|nvlink|pcie|cxl|gen-z|rdma|omni-?path|roce)\b", name, re.I):
        return "ultra_low"
    if re.search(r"\b(satellite|deep space|cubesat|haps|submarine|delay-tolerant|store-and-forward|sneakernet|troposcatter|hf)\b", name, re.I):
        return "high"
    if medium == "optical":
        return "ultra_low"
    if medium == "wireless":
        return "low"
    return "low"


def pick_security(name: str) -> str:
    if re.search(r"\b(military|classified|secure enclave|cross-domain|data diode|one-way|tactical|nuclear|crypto-isolated)\b", name, re.I):
        return "military"
    if re.search(r"\b(zero-trust|bastion|hsm|financial|trading|banking|medical|pharmaceutical)\b", name, re.I):
        return "high"
    if re.search(r"\b(air-gap|air-?gapped|quarantine|honeynet|deception)\b", name, re.I):
        return "high"
    if re.search(r"\b(legacy|arcnet|token ring|appletalk|ipx|fddi|x\.25|serial|spi|i2c)\b", name, re.I):
        return "none"
    return "medium"


def build_entry(display_name: str, used_ids: set[str]) -> dict:
    base = slug(display_name)
    tid = base
    i = 2
    while tid in used_ids:
        tid = f"{base}_{i}"
        i += 1
    used_ids.add(tid)

    cat = pick_category(display_name)
    medium = pick_medium(display_name)
    lat = pick_latency(display_name, medium)
    sec = pick_security(display_name)

    return {
        "id": tid,
        "name": display_name,
        "category": cat,
        "subcategory": None,
        "description": f"{display_name} — auto-generated transport definition.",
        "layer": 2,
        "medium": medium,
        "typical_bandwidth": "varies",
        "typical_range": "varies",
        "latency_class": lat,
        "detection_method": "hardware_probe",
        "adapter_family": "generic",
        "is_common": False,
        "requires_hardware": medium != "virtual",
        "duplex": "full",
        "supports_multicast": True,
        "supports_broadcast": True,
        "max_nodes": None,
        "security_level": sec,
    }


def main() -> None:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    is_list = isinstance(raw, list)
    entries: list[dict] = raw if is_list else raw.get("transports", [])

    existing_ids = {e["id"] for e in entries}
    existing_names = {norm(e.get("name", "")) for e in entries}

    user_lines: list[str] = [
        ln.strip()
        for ln in USER_LIST.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]

    added: list[dict] = []
    skipped = 0

    for line in user_lines:
        key = norm(line)
        if key in existing_names:
            skipped += 1
            continue
        entry = build_entry(line, existing_ids)
        entries.append(entry)
        existing_names.add(key)
        added.append(entry)

    payload: list | dict = entries if is_list else {**raw, "transports": entries}
    CATALOG.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"User list size: {len(user_lines)}")
    print(f"Already present (skipped): {skipped}")
    print(f"Newly added: {len(added)}")
    print(f"Final catalog size: {len(entries)}")

    # Report category distribution of new ones
    by_cat: dict[str, int] = {}
    for e in added:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + 1
    print("\nAdded by category:")
    for cat, n in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}")


if __name__ == "__main__":
    main()
