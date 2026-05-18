"""Generate 18 new transport adapter modules + 7 detection rule additions.

Run once from the project root:
    venv/Scripts/python.exe tools/gen_adapters.py
"""

from __future__ import annotations

import json
import os

ADAPTERS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "app", "transports", "adapters"
)
RULES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "transports", "config",
    "detection_rules.json"
)
ADAPTERS_DIR = os.path.normpath(ADAPTERS_DIR)
RULES_PATH = os.path.normpath(RULES_PATH)

TEMPLATE = '''"""
{display_name} transport adapter.

Family: ``{family}``

Provides detection of {family}-class network interfaces by matching
keywords on psutil interface names plus serial-port descriptions.
``connect()`` / ``send()`` / ``receive()`` open a TCP-style stream over
whatever underlying device the OS exposed; for non-IP buses (RS-485,
CAN, audio) it falls back to pyserial when the port matches.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from app.core.logging import get_logger
from app.transports.adapters.base import BaseTransportAdapter

logger = get_logger(__name__)


class {class_name}(BaseTransportAdapter):
    family = "{family}"
    display_name = "{display_name}"
    keywords = {keywords!r}

    async def detect(self) -> list[dict[str, Any]]:
        detected: list[dict[str, Any]] = []

        # 1) IP interfaces whose name hints at this transport family
        try:
            import psutil
            for ifname, addrs in psutil.net_if_addrs().items():
                low = ifname.lower()
                if any(kw in low for kw in self.keywords):
                    ip = next(
                        (a.address for a in addrs if a.family == socket.AF_INET),
                        None,
                    )
                    detected.append({{
                        "interface": ifname,
                        "type": self.family,
                        "ip": ip,
                        "status": "available",
                        "metadata": {{"medium": self.family, "source": "psutil"}},
                    }})
        except Exception as exc:
            logger.debug("{family}_psutil_detect_failed", error=str(exc))

        # 2) Serial ports whose description hints at this transport
        try:
            import serial.tools.list_ports
            for port, desc, hwid in serial.tools.list_ports.comports():
                low = (desc or "").lower()
                if any(kw in low for kw in self.keywords):
                    detected.append({{
                        "interface": port,
                        "port": port,
                        "type": self.family,
                        "description": desc,
                        "status": "available",
                        "metadata": {{"medium": self.family, "source": "serial"}},
                    }})
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("{family}_serial_detect_failed", error=str(exc))

        logger.info("{family}_detection_complete", count=len(detected))
        return detected

    async def connect(self, interface: str, config: dict[str, Any]) -> Any:
        # Heuristic: COM*/dev/tty* paths go through pyserial; everything
        # else is treated as an IP-bound interface and we open a TCP
        # stream against ``config['host']:config['port']``.
        if interface.upper().startswith("COM") or interface.startswith("/dev/tty"):
            try:
                import serial
                conn = serial.Serial(
                    interface,
                    baudrate=config.get("baudrate", 9600),
                    timeout=config.get("timeout", 1.0),
                )
                logger.info("{family}_serial_connected", interface=interface)
                return conn
            except Exception as exc:
                logger.error("{family}_serial_connect_failed", error=str(exc))
                raise

        host = config.get("host", "127.0.0.1")
        port = int(config.get("port", 0))
        if not port:
            raise ValueError(f"{{self.family}} TCP connect requires config['port']")
        reader, writer = await asyncio.open_connection(host, port)
        logger.info("{family}_tcp_connected", host=host, port=port)
        return (reader, writer)

    async def disconnect(self, connection_id: Any) -> bool:
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                _, writer = connection_id
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            elif hasattr(connection_id, "close"):
                connection_id.close()
            return True
        except Exception as exc:
            logger.error("{family}_disconnect_failed", error=str(exc))
            return False

    async def send(self, connection_id: Any, data: bytes) -> int:
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                _, writer = connection_id
                writer.write(data)
                await writer.drain()
                return len(data)
            if hasattr(connection_id, "write"):
                return int(connection_id.write(data) or 0)
            return 0
        except Exception as exc:
            logger.error("{family}_send_failed", error=str(exc))
            return 0

    async def receive(
        self, connection_id: Any, buffer_size: int = 65536
    ) -> bytes:
        try:
            if isinstance(connection_id, tuple) and len(connection_id) == 2:
                reader, _ = connection_id
                return await reader.read(buffer_size)
            if hasattr(connection_id, "read"):
                return connection_id.read(buffer_size)
            return b""
        except Exception as exc:
            logger.error("{family}_receive_failed", error=str(exc))
            return b""

    def is_available(self) -> bool:
        try:
            import psutil  # noqa: F401
            return True
        except ImportError:
            return False
'''

NEW_ADAPTERS = [
    ("military_defense", "Military / Defense", "MilitaryAdapter",
        ["mil-", "milstd", "havequick", "link16", "link22", "saatcom",
         "sincgars", "tactical-ip"]),
    ("maritime_underwater", "Maritime / Underwater", "MaritimeAdapter",
        ["maritime", "subsea", "underwater", "modem-um", "ais", "dvl",
         "sonar"]),
    ("energy_grid", "Energy Grid", "EnergyGridAdapter",
        ["dnp3", "iec61850", "iec104", "modbus-grid", "smartgrid",
         "scada-grid", "amr"]),
    ("medical", "Medical", "MedicalAdapter",
        ["hl7", "dicom", "medical", "ekg", "ecg", "ventilator",
         "infusion", "icu-net"]),
    ("broadcast_media", "Broadcast Media", "BroadcastMediaAdapter",
        ["smpte", "ndi", "st2110", "aes67", "broadcast", "vsf-tr",
         "audio-broadcast"]),
    ("topology", "Topology", "TopologyAdapter",
        ["topology", "graph", "spine", "leaf", "tor"]),
    ("deep_space", "Deep Space", "DeepSpaceAdapter",
        ["dsn", "deep-space", "dtn", "bp-bundle", "ccsds", "dsp"]),
    ("financial_trading", "Financial / Trading", "FinancialTradingAdapter",
        ["fix", "itch", "ouch", "trading", "marketdata", "exchange"]),
    ("quantum_experimental", "Quantum (Experimental)", "QuantumAdapter",
        ["qkd", "quantum", "qnet", "entangle"]),
    ("railway", "Railway", "RailwayAdapter",
        ["ertms", "etcs", "rail-", "railnet", "tcms", "subway-net"]),
    ("mining_underground", "Mining / Underground", "MiningAdapter",
        ["mining", "shaft", "tunnel-net", "leakyfeeder",
         "underground-mesh"]),
    ("drone_uav", "Drone / UAV", "DroneUAVAdapter",
        ["drone", "uav", "mavlink", "telemetry", "ardupilot", "px4"]),
    ("wireless_bridge", "Wireless Bridge", "WirelessBridgeAdapter",
        ["bridge", "ptp-link", "ptmp", "wisp", "60ghz"]),
    ("automotive", "Automotive", "AutomotiveAdapter",
        ["can", "obd", "automotive", "candan", "vehicle-bus",
         "auto-ethernet"]),
    ("aviation", "Aviation", "AviationAdapter",
        ["arinc", "afdx", "ads-b", "acars", "aviation", "cpdlc"]),
    ("emergency_public_safety", "Emergency / Public Safety",
        "EmergencyAdapter",
        ["p25", "tetra", "dmr", "fnet", "first-responder",
         "publicsafety"]),
    ("nuclear", "Nuclear", "NuclearAdapter",
        ["nuclear-net", "rad", "reactor", "iaea", "safety-bus"]),
    ("acoustic", "Acoustic / Underwater", "AcousticAdapter",
        ["acoustic", "modem-acoustic", "hydrophone", "ultrasound-net"]),
]


def main() -> None:
    written = 0
    skipped = 0
    for family, display, cls, keywords in NEW_ADAPTERS:
        path = os.path.join(ADAPTERS_DIR, f"{family}.py")
        if os.path.exists(path):
            print(f"[=] {family}.py (kept — already exists)")
            skipped += 1
            continue
        content = TEMPLATE.format(
            family=family,
            display_name=display,
            class_name=cls,
            keywords=keywords,
        )
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        print(f"[+] {family}.py")
        written += 1

    print(f"\nAdapter modules written: {written}, kept: {skipped}")
    total = len([
        f for f in os.listdir(ADAPTERS_DIR)
        if f.endswith(".py") and f not in ("__init__.py", "base.py")
    ])
    print(f"Total adapters in dir: {total}")

    # Detection rules
    with open(RULES_PATH, encoding="utf-8") as f:
        rules = json.load(f)

    new_rules = {
        "automotive": {
            "keywords": ["can", "obd", "auto", "vehicle", "automotive"],
            "interfaces": ["can*", "vcan*", "obd*"],
            "default_protocol": "CAN-bus",
            "lan_mappable": False,
        },
        "aviation": {
            "keywords": ["arinc", "afdx", "ads-b", "acars", "cpdlc",
                         "aviation"],
            "interfaces": ["arinc*", "afdx*"],
            "default_protocol": "ARINC-664/AFDX",
            "lan_mappable": True,
        },
        "emergency_public_safety": {
            "keywords": ["p25", "tetra", "dmr", "first-responder",
                         "publicsafety"],
            "interfaces": ["p25*", "tetra*"],
            "default_protocol": "P25/TETRA",
            "lan_mappable": False,
        },
        "nuclear": {
            "keywords": ["nuclear", "reactor", "iaea", "safety-bus", "rad"],
            "interfaces": ["nuc*", "safety*"],
            "default_protocol": "Nuclear-Safety-Bus",
            "lan_mappable": False,
        },
        "acoustic": {
            "keywords": ["acoustic", "hydrophone", "ultrasound", "sonar"],
            "interfaces": ["acoustic*", "hydro*"],
            "default_protocol": "Acoustic-Modem",
            "lan_mappable": False,
        },
        "satellite_aerospace": {
            "keywords": ["satellite", "gps", "gnss", "inmarsat", "iridium",
                         "starlink"],
            "interfaces": ["sat*", "gnss*"],
            "default_protocol": "Satellite-IP",
            "lan_mappable": True,
        },
        "serial_bus": {
            "keywords": ["serial", "rs232", "rs485", "rs422", "uart",
                         "modbus-rtu"],
            "interfaces": ["COM*", "/dev/tty*", "/dev/ttyUSB*", "/dev/ttyS*"],
            "default_protocol": "Serial-RS-x",
            "lan_mappable": False,
        },
    }

    added = 0
    for cat, rule in new_rules.items():
        if cat not in rules:
            rules[cat] = rule
            print(f"[+] detection rule: {cat}")
            added += 1
        else:
            print(f"[=] detection rule for {cat} kept")

    with open(RULES_PATH, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)

    print(f"\nDetection rules: was 55, now {len(rules)} (+{added})")


if __name__ == "__main__":
    main()
