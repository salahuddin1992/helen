"""Re-assign proper categories to the 229 'custom' transports.

Uses a second-pass keyword matcher tuned to the names that fell through the
initial heuristics in merge_user_transports.py.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CATALOG = Path(__file__).resolve().parent.parent / "app" / "transports" / "config" / "transport_catalog.json"

# Order matters — first match wins. More specific patterns on top.
RULES: list[tuple[re.Pattern, str]] = [
    # ── Fronthaul / cellular ──
    (re.compile(r"\b(cpri|ecpri|obsai|fronthaul|midhaul|backhaul|cloud-?ran|v?ran|o-?ran|small cell|picocell|femtocell|macrocell|neutral host|das)\b", re.I), "cellular_private"),
    # ── SONET / transport nets ──
    (re.compile(r"\b(sonet|sdh|otn|ptn|optical transport|packet transport|mpls-?tp|segment routing|pbb-te|rpr|resilient packet ring|metro ethernet|carrier ethernet|leased line|t1|e1|ds3)\b", re.I), "wan_private"),
    # ── Industrial / plant / oilfield ──
    (re.compile(r"\b(factory|oilfield|wellhead|pipeline|mission|foundry|cleanroom|photolithography|semiconductor|automotive assembly|battery plant|beverage|food processing|pharmaceutical plant|water treatment|wastewater|desalination|dam control|power plant|packaging line|printing production|ttethernet|safe ethernet|process historian|machine vision|cnc|servo|motion control|safety instrumented|alarm network|operator hmi|engineering workstation|maintenance laptop|industrial private|single pair|ethernet-apl|podl|powerlink|varan|fl-net|mechatrolink|ethersound|cobranet)\b", re.I), "industrial"),
    # ── Time sensitive / determinism ──
    (re.compile(r"\b(hsr|prp|tsn|avb|ptp|sync|ntp|white rabbit|deterministic|time-sensitive|lossless ethernet)\b", re.I), "time_sensitive"),
    # ── Topology ──
    (re.compile(r"\b(ring network|dual ring|daisy chain|hub-and-spoke|star topology|tree topology|bus topology|hypercube|torus|dragonfly|fat-?tree|clos|spine-?leaf|leaf-spine|full mesh|partial mesh|non-blocking|rearrangement|any-to-any)\b", re.I), "topology"),
    # ── Datacenter fabrics ──
    (re.compile(r"\b(top-of-rack|end-of-row|middle-of-row|rack interconnect|disaggregated|composable|spine|leaf|cable-free backplane|passive backplane|optical backplane|fabricpath|stackwise|virtual chassis|multi-chassis|backplane)\b", re.I), "datacenter_fabric"),
    # ── Overlay / SDN / tunnels ──
    (re.compile(r"\b(sdn|openflow|p4 programmable|intent-?based|nfv|rina|ndn|scion|locator/id|evpl|e-line|e-lan|e-tree|gse|capwap|overlay|underlay|virtualization tunnel|geneve|vxlan|evpn|gre|l2tp|ipsec|wireguard|trill|spb|segment routing|vpls|pseudowire|mpls-?tp|control plane|data plane)\b", re.I), "overlay_tunnel"),
    # ── Service mesh / event bus ──
    (re.compile(r"\b(service mesh|message bus|event bus|pub/sub|message queue|command bus|request/reply|brokerless|dds middleware|stream processing|event-driven|kubernetes|cluster service|cni overlay|heartbeat|replication network|backup network|gossip|consensus|leader election|peer discovery|local name|mdns|service advertisement)\b", re.I), "service_overlay"),
    # ── Edge / fog ──
    (re.compile(r"\b(edge computing|fog network|edge cache|local cdn|multicast distribution|federated edge|swarm edge|edge ai|sensor fusion|digital twin|render farm|render storage|stream processing)\b", re.I), "service_overlay"),
    # ── Security isolated ──
    (re.compile(r"\b(air-?gap|closed loop|quarantine|bastion|jump host|secure enclave|classified|cross-domain|data diode|one-way|sandbox|honeynet|deception|red team|blue team|malware detonation|isolated|crypto-?isolated|zero-?trust|microsegmented|east-west|perimeterless|policy-driven|application-aware|service-chained|identity-based|intent-segmented|trusted execution|remote attestation|secure bootstrapping|certificate distribution|key management|hsm|backup key|secure overlay)\b", re.I), "security_isolated"),
    # ── Building / facility ──
    (re.compile(r"\b(building|elevator|access control|cctv|intercom|nurse call|classroom|dorm|library|hotel|resort|cruise|theme park|museum|casino|stadium|venue|recreation|courtroom|prison|archive preservation|campus|streetlight)\b", re.I), "building_campus"),
    # ── SCADA / utility ──
    (re.compile(r"\b(scada|plc|rtu|ied|protection relay|synchrophasor|pmu|metering|ami|substation|utility|distribution automation|pipeline monitoring|wellhead telemetry|district heating|water meter|gas meter)\b", re.I), "scada_utility"),
    # ── Emergency / public safety ──
    (re.compile(r"\b(emergency|dispatch radio|roip|tactical data|command post|tactical operations|battlefield|manpack|sdr mesh|isr|border surveillance|coastal|public safety|first responder|fire station|police|ambulance|disaster recovery|eoc|mutual aid|incident)\b", re.I), "tactical_emergency"),
    # ── Maritime ──
    (re.compile(r"\b(shipboard|submarine|maritime|harbor|marine buoy|oceanographic|aquaculture|port terminal|container yard|coastal|cruise ship)\b", re.I), "maritime_underwater"),
    # ── Mining ──
    (re.compile(r"\b(mining|mine|borehole|seismic|underground|tunnel communication)\b", re.I), "mining_underground"),
    # ── Railway ──
    (re.compile(r"\b(rail|train|cbtc|etcs|positive train|rail signaling)\b", re.I), "railway"),
    # ── Automotive / vehicular ──
    (re.compile(r"\b(vehicular|v2x|roadside|ev charging|charging depot|fleet telematics|taxi dispatch|bus depot|traffic signal|tolling|parking|workshop equipment|depot maintenance)\b", re.I), "transport_vehicle"),
    # ── Broadcast / media ──
    (re.compile(r"\b(broadcast|studio|newsroom|live event|color grading|foley|post-production|motion capture|ar collaboration|vr free-?roam|digital signage|iptv|conference av|room control|hearing assistance|translation booth|esports)\b", re.I), "broadcast_media"),
    # ── Financial ──
    (re.compile(r"\b(trading|market data|settlement|clearing|atm banking|core banking|pos payment|kiosk|branch interconnect|retail edge|franchise|cold chain)\b", re.I), "financial_trading"),
    # ── Medical ──
    (re.compile(r"\b(medical|hospital|pacs|dicom|pharmacy|laboratory instrument|icu|ambulance telemetry)\b", re.I), "medical"),
    # ── Energy / grid ──
    (re.compile(r"\b(smart grid|microgrid|bess|solar farm|wind farm|power plant control|substation automation|ev charging|charging depot|neighborhood area|field area|head-end utility)\b", re.I), "energy_grid"),
    # ── Deep space / aerospace ──
    (re.compile(r"\b(deep space|inter-satellite|cubesat|haps|balloon|near-space|ground segment|telemetry network|telecommand|tracking network|range instrumentation)\b", re.I), "deep_space"),
    # ── Quantum / research ──
    (re.compile(r"\b(quantum|cryogenic|particle lab|telescope|observatory|research instrument|testbed)\b", re.I), "quantum_experimental"),
    # ── Drone / UAV ──
    (re.compile(r"\b(drone|uav|uas)\b", re.I), "drone_uav"),
    # ── HPC / compute fabric ──
    (re.compile(r"\b(hpc|rdma|roce|omni-?path|nvlink|pcie|memory fabric|persistent memory|accelerator|tpu|ai training|ai inference|gpu fabric|render|cache coherency)\b", re.I), "high_performance"),
    # ── Management / OOB ──
    (re.compile(r"\b(management|ipmi|bmc|redfish|baseboard|chassis management|out-of-band|kvm over ip|serial console|cwmp|observability|siem|telemetry aggregation|metrics collection|log shipping|trace collection|security monitoring|compliance logging|audit collection|patch mirror|repository mirror|package cache|local registry|bare-?metal|pxe|imaging network|provisioning)\b", re.I), "management"),
    # ── Mesh / ad-hoc ──
    (re.compile(r"\b(mesh|manet|vanet|batman|babel|cjdns|community mesh|sneakernet|store-and-forward|delay-?tolerant|ferry-based|courier data|opportunistic|backpack node|suitcase server|pop-up|mobile command|rapid deployment|expeditionary|harsh environment|ruggedized|portable edge|neighborhood watch|rural broadband)\b", re.I), "mesh"),
    # ── Ethernet flavors ──
    (re.compile(r"\b(10base|100base|1000base|10gbase|25gbe|40gbe|100gbe|200gbe|400gbe|800gbe|single pair ethernet|metro ethernet|ethernet over|pseudowire|coaxial ethernet)\b", re.I), "ethernet"),
    # ── IoT sensor / personal area ──
    (re.compile(r"\b(personal area|body area|wireless sensor|xbee|rtls|uwb tracking|ble beacon|asset tracking|warehouse tag|cold storage sensor|freezer monitoring|weather station|hydrology|agriculture precision|irrigation|greenhouse|livestock|environmental monitoring|rfid|inventory rfid|library rfid)\b", re.I), "iot_sensor"),
    # ── Storage network ──
    (re.compile(r"\b(storage area|san|fcoe|iscsi|nvme over|storage backplane|database replication|storage replication|fibre channel|archive lto|media asset management|archive replication|long-?term preservation|cold backup|warm standby|hot standby|active-active|active-passive|multi-primary|local failover|site resilience)\b", re.I), "storage_network"),
    # ── Campus / wan private ──
    (re.compile(r"\b(geo-?redundant|multi-?site|building-to-building|metro dark fiber|inter-building|cross-site replication|smart building|branch interconnect|metro dark|cross-site)\b", re.I), "wan_private"),
    # ── Specialty vertical (fallback for unique items) ──
    (re.compile(r"\b(election|census|court records|land registry|municipal|public works|waste collection|district heating|dorm access|campus security|freezer|cold chain|postal automation|parcel sortation|logistics hub|community mesh|tribal|island|mountain|desert|retail|game server|lan party|simulation cluster|vr|ar|3d printer|fab lab|maker space|robotics competition|esports|digital signage|newsroom|studio|production network)\b", re.I), "specialty_vertical"),
    # ── Second-pass catches for stubborn 'custom' entries ──
    (re.compile(r"\b(fixed wireless access|open radio access|distributed antenna|distributed radio|indoor das)\b", re.I), "cellular_private"),
    (re.compile(r"\b(optical circuit-?switched|burst-?switched|hybrid packet-?optical)\b", re.I), "fiber"),
    (re.compile(r"\b(packet capture|tap aggregation|mirror/?span|forensic acquisition|removable media|threat intel|vendor remote access|local update distribution|patch validation|recovery network|boot network)\b", re.I), "security_isolated"),
    (re.compile(r"\b(build farm|ci/cd runner|artifact distribution|container image distribution|pilot deployment|blue-?green|canary|rollback|immutable infrastructure|staging pre-production)\b", re.I), "service_overlay"),
    (re.compile(r"\b(ot/it convergence|machine-to-machine|control bus|data bus|high-?speed camera|amr warehouse|warehouse control)\b", re.I), "industrial"),
    (re.compile(r"\b(extranet|supplier integration|b2b interconnect|edi network|core banking|carrier-?grade nat|core fabric|supercore|regional edge|access aggregation|local breakout|edge transport|cloudless local fabric)\b", re.I), "wan_private"),
    (re.compile(r"\b(telepresence|collaboration room|secure voice|push-to-talk|roaming-free|local presence|offline multiplayer|dispatch operations|continuity of operations|autonomous recovery|sensor-to-cloudless)\b", re.I), "service_overlay"),
    (re.compile(r"\b(distributed ledger|blockchain|consortium network)\b", re.I), "service_overlay"),
    (re.compile(r"\b(appliance-to-appliance|west-east access|southbound industrial|northbound integration)\b", re.I), "topology"),
]


def main() -> None:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    is_list = isinstance(raw, list)
    entries = raw if is_list else raw.get("transports", [])

    customs = [e for e in entries if e.get("category") == "custom"]
    print(f"Custom before: {len(customs)}")

    changes = 0
    still_custom = 0
    by_new_cat: dict[str, int] = {}
    for e in entries:
        if e.get("category") != "custom":
            continue
        name = e.get("name", "")
        matched = False
        for pat, cat in RULES:
            if pat.search(name):
                e["category"] = cat
                by_new_cat[cat] = by_new_cat.get(cat, 0) + 1
                changes += 1
                matched = True
                break
        if not matched:
            still_custom += 1

    payload = entries if is_list else {**raw, "transports": entries}
    CATALOG.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Re-categorized: {changes}")
    print(f"Still custom: {still_custom}")
    print("\nRe-assigned by new category:")
    for cat, n in sorted(by_new_cat.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}")


if __name__ == "__main__":
    main()
