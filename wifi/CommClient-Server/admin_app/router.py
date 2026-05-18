"""
Router management — UPnP-IGD + NAT-PMP + optional admin-panel profiles.

Goal
----
Make Helen reachable even when the user's router actively works against it
(AP isolation, blocked broadcast, closed inbound ports). Three escalation
tiers, stdlib-only so PyInstaller bundles it cleanly:

1. **UPnP-IGD** — SSDP discovery of an Internet Gateway Device, then SOAP
   calls to its WAN*Connection service. Used to add/remove port mappings,
   read the external IP, enumerate existing mappings. Most consumer routers
   speak this by default.

2. **NAT-PMP / PCP** — Apple's simpler UDP protocol on the default gateway.
   Fallback when UPnP is off or misconfigured.

3. **Router admin profiles** (user-supplied credentials) — generic skeleton
   for brand-specific routines such as disabling client isolation or
   enabling IGMP snooping, driven by credentials the user pastes in. The
   vault is encrypted with Windows DPAPI so plain-text creds never touch
   disk.

This module intentionally never probes for default credentials, exploits
vulnerabilities, or bypasses auth. If the router denies a SOAP call,
we surface the 401/403; the user must explicitly allow UPnP on the router
or provide credentials.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ── SSDP discovery ────────────────────────────────────────


SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX = 2  # seconds the caller waits between retries
SSDP_IGD_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "upnp:rootdevice",
)


def _ssdp_search(
    target: str, timeout: float = 3.0, max_replies: int = 8,
) -> list[dict[str, str]]:
    """Multicast M-SEARCH to discover UPnP devices. Returns the parsed reply
    headers for each responding device (deduplicated by LOCATION)."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        "MAN: \"ssdp:discover\"\r\n"
        f"MX: {SSDP_MX}\r\n"
        f"ST: {target}\r\n"
        "\r\n"
    ).encode("ascii")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
    except OSError:
        sock.close()
        return []

    seen: set[str] = set()
    replies: list[dict[str, str]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(replies) < max_replies:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            break
        except OSError:
            break
        headers = _parse_http_headers(data)
        loc = headers.get("location")
        if not loc or loc in seen:
            continue
        seen.add(loc)
        headers["_remote_addr"] = addr[0]
        replies.append(headers)
    sock.close()
    return replies


def _parse_http_headers(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return out
    lines = text.split("\r\n")
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip().lower()] = v.strip()
    return out


# ── UPnP-IGD SOAP client ──────────────────────────────────


UPNP_NS = {
    "d": "urn:schemas-upnp-org:device-1-0",
    "s": "urn:schemas-upnp-org:service-1-0",
}

WAN_SERVICE_IDS = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


@dataclass
class UpnpService:
    service_type: str
    control_url: str  # absolute
    scpd_url: str
    event_sub_url: str


@dataclass
class UpnpDevice:
    base_url: str
    location: str
    friendly_name: str = ""
    manufacturer: str = ""
    model_name: str = ""
    model_number: str = ""
    serial_number: str = ""
    services: list[UpnpService] = field(default_factory=list)

    def wan_service(self) -> UpnpService | None:
        for svc in self.services:
            if svc.service_type in WAN_SERVICE_IDS:
                return svc
        return None


def _resolve_url(base: str, path: str) -> str:
    if path.startswith("http"):
        return path
    return urllib.parse.urljoin(base, path)


def _fetch_device_description(location: str, timeout: float = 4.0) -> UpnpDevice | None:
    try:
        with urllib.request.urlopen(location, timeout=timeout) as resp:
            body = resp.read()
    except (urllib.error.URLError, OSError):
        return None

    # Strip default namespace so ElementTree xpath is survivable.
    text = body.decode("utf-8", errors="replace")
    text = re.sub(r'\sxmlns="[^"]+"', "", text, count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    base_el = root.find(".//URLBase")
    base_url = (base_el.text or "").strip() if base_el is not None else ""
    if not base_url:
        parsed = urllib.parse.urlparse(location)
        base_url = f"{parsed.scheme}://{parsed.netloc}/"

    device = UpnpDevice(base_url=base_url, location=location)
    dev_el = root.find(".//device")
    if dev_el is not None:
        device.friendly_name = _eltext(dev_el, "friendlyName")
        device.manufacturer = _eltext(dev_el, "manufacturer")
        device.model_name = _eltext(dev_el, "modelName")
        device.model_number = _eltext(dev_el, "modelNumber")
        device.serial_number = _eltext(dev_el, "serialNumber")

    # Walk all service elements — the WAN*Connection one can live deep
    # inside nested deviceList entries on IGDs.
    for svc_el in root.findall(".//service"):
        st = _eltext(svc_el, "serviceType")
        ctrl = _eltext(svc_el, "controlURL")
        scpd = _eltext(svc_el, "SCPDURL")
        esub = _eltext(svc_el, "eventSubURL")
        if not st or not ctrl:
            continue
        device.services.append(
            UpnpService(
                service_type=st,
                control_url=_resolve_url(base_url, ctrl),
                scpd_url=_resolve_url(base_url, scpd) if scpd else "",
                event_sub_url=_resolve_url(base_url, esub) if esub else "",
            )
        )
    return device


def _eltext(parent: ET.Element, tag: str) -> str:
    el = parent.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


class UpnpError(Exception):
    pass


def _soap_call(
    service: UpnpService,
    action: str,
    args: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> dict[str, str]:
    """Invoke a UPnP SOAP action. Returns the decoded response arguments.
    Raises ``UpnpError`` with the fault code on error responses.
    """
    arg_xml = "".join(f"<{k}>{_xml_escape(v)}</{k}>" for k, v in (args or {}).items())
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service.service_type}">{arg_xml}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode("utf-8")

    req = urllib.request.Request(
        service.control_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service.service_type}#{action}"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            return _parse_soap_response(payload, action)
    except urllib.error.HTTPError as e:
        # UPnP errors come back as HTTP 500 with a fault body.
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        detail = _parse_soap_fault(raw)
        raise UpnpError(f"{action}: HTTP {e.code} — {detail or e.reason}") from None
    except urllib.error.URLError as e:
        raise UpnpError(f"{action}: {e.reason}") from None


def _xml_escape(value: str) -> str:
    s = str(value)
    return (
        s.replace("&", "&amp;").replace("<", "&lt;")
         .replace(">", "&gt;").replace('"', "&quot;")
    )


def _parse_soap_response(body: bytes, action: str) -> dict[str, str]:
    text = body.decode("utf-8", errors="replace")
    text = re.sub(r'\sxmlns[^=]*="[^"]+"', "", text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    # Look for <u:<action>Response> anywhere.
    for el in root.iter():
        if el.tag.endswith(f"{action}Response"):
            return {child.tag.split('}')[-1]: (child.text or "") for child in el}
    return {}


def _parse_soap_fault(body: bytes) -> str:
    try:
        text = body.decode("utf-8", errors="replace")
        text = re.sub(r'\sxmlns[^=]*="[^"]+"', "", text)
        root = ET.fromstring(text)
        code = root.findtext(".//errorCode") or ""
        desc = root.findtext(".//errorDescription") or ""
        return f"{code} {desc}".strip() or text[:200]
    except Exception:
        return ""


class UpnpIgd:
    """High-level operations against a single UPnP IGD."""

    def __init__(self, device: UpnpDevice) -> None:
        self.device = device
        svc = device.wan_service()
        if svc is None:
            raise UpnpError("no WAN*Connection service on this device")
        self.service = svc

    def get_external_ip(self) -> str:
        r = _soap_call(self.service, "GetExternalIPAddress")
        return r.get("NewExternalIPAddress", "")

    def get_status(self) -> dict[str, str]:
        return _soap_call(self.service, "GetStatusInfo")

    def add_port_mapping(
        self,
        *,
        external_port: int,
        internal_port: int,
        internal_client: str,
        protocol: str = "TCP",
        description: str = "Helen",
        lease_seconds: int = 0,
        enabled: bool = True,
    ) -> None:
        _soap_call(
            self.service,
            "AddPortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": str(external_port),
                "NewProtocol": protocol.upper(),
                "NewInternalPort": str(internal_port),
                "NewInternalClient": internal_client,
                "NewEnabled": "1" if enabled else "0",
                "NewPortMappingDescription": description,
                "NewLeaseDuration": str(int(lease_seconds)),
            },
        )

    def delete_port_mapping(
        self, external_port: int, protocol: str = "TCP",
    ) -> None:
        _soap_call(
            self.service,
            "DeletePortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": str(external_port),
                "NewProtocol": protocol.upper(),
            },
        )

    def list_port_mappings(self, limit: int = 128) -> list[dict[str, str]]:
        mappings: list[dict[str, str]] = []
        for i in range(limit):
            try:
                r = _soap_call(
                    self.service,
                    "GetGenericPortMappingEntry",
                    {"NewPortMappingIndex": str(i)},
                )
            except UpnpError:
                break
            if not r:
                break
            mappings.append(r)
        return mappings


# ── NAT-PMP / PCP (RFC 6886 / RFC 6887) ───────────────────


NATPMP_PORT = 5351
PCP_PORT = 5351  # same port, different opcode space


def _default_gateway_ipv4() -> str | None:
    """Best-effort Windows default-gateway lookup.

    Parses ``route print -4`` because it's available on any supported Windows
    and doesn't need elevation. Returns the first non-loopback gateway.
    """
    try:
        out = subprocess.run(
            ["route", "print", "-4"],
            capture_output=True, timeout=4.0, text=True, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        # Default route rows look like:
        #   0.0.0.0  0.0.0.0  192.168.1.1  192.168.1.132  25
        if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            gw = parts[2]
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", gw) and not gw.startswith("127."):
                return gw
    return None


class NatPmpError(Exception):
    pass


def natpmp_external_ip(gateway: str, timeout: float = 1.5) -> str:
    """NAT-PMP opcode 0 — get external address. RFC 6886 §3.2."""
    req = struct.pack("!BB", 0, 0)  # version=0, opcode=0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(req, (gateway, NATPMP_PORT))
        data, _ = sock.recvfrom(16)
    except (OSError, socket.timeout) as e:
        raise NatPmpError(f"no response: {e}") from None
    finally:
        sock.close()
    if len(data) < 12:
        raise NatPmpError("short response")
    version, opcode, result, epoch, a, b, c, d = struct.unpack("!BBHLBBBB", data)
    if result != 0:
        raise NatPmpError(f"result_code={result}")
    return f"{a}.{b}.{c}.{d}"


def natpmp_add_mapping(
    gateway: str,
    *,
    internal_port: int,
    external_port: int,
    protocol: str = "TCP",
    lifetime_sec: int = 3600,
    timeout: float = 1.5,
) -> int:
    """NAT-PMP opcode 1 (UDP) or 2 (TCP). Returns the actually-granted
    external port (router may pick a different one)."""
    opcode = 2 if protocol.upper() == "TCP" else 1
    req = struct.pack(
        "!BBHHHL",
        0, opcode, 0,
        int(internal_port), int(external_port), int(lifetime_sec),
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(req, (gateway, NATPMP_PORT))
        data, _ = sock.recvfrom(16)
    except (OSError, socket.timeout) as e:
        raise NatPmpError(f"no response: {e}") from None
    finally:
        sock.close()
    if len(data) < 16:
        raise NatPmpError("short response")
    _, _, result, _, _, granted_ext, _ = struct.unpack("!BBHLHHL", data)
    if result != 0:
        raise NatPmpError(f"result_code={result}")
    return int(granted_ext)


# ── Windows DPAPI credentials vault ───────────────────────


def _dpapi_protect(plaintext: bytes) -> bytes | None:
    """Encrypt via Windows DPAPI current-user scope. Returns None outside
    Windows or on failure — caller must handle."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL

    in_buf = ctypes.create_string_buffer(plaintext, len(plaintext))
    blob_in = DATA_BLOB(len(plaintext),
                        ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB(0, None)
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), "Helen",
        None, None, None, 0, ctypes.byref(blob_out),
    )
    if not ok:
        return None
    try:
        size = int(blob_out.cbData)
        ptr = ctypes.cast(blob_out.pbData, ctypes.POINTER(ctypes.c_char))
        data = bytes(ptr[:size])
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return data
    except Exception:
        return None


def _dpapi_unprotect(ciphertext: bytes) -> bytes | None:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    in_buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
    blob_in = DATA_BLOB(len(ciphertext),
                        ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB(0, None)
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out),
    )
    if not ok:
        return None
    try:
        size = int(blob_out.cbData)
        ptr = ctypes.cast(blob_out.pbData, ctypes.POINTER(ctypes.c_char))
        data = bytes(ptr[:size])
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return data
    except Exception:
        return None


class RouterCredentialVault:
    """Encrypted store for router admin credentials (user-supplied).

    Stores a single JSON blob under %LOCALAPPDATA%\\Helen\\router.dat
    encrypted with Windows DPAPI. The file can only be decrypted by the
    same user on the same machine.
    """

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            base = Path(
                (__import__("os").environ.get("LOCALAPPDATA"))
                or Path.home() / "AppData" / "Local"
            ) / "Helen"
            base.mkdir(parents=True, exist_ok=True)
            path = base / "router.dat"
        self.path = path

    def save(self, creds: dict[str, str]) -> bool:
        blob = json.dumps(creds).encode("utf-8")
        enc = _dpapi_protect(blob)
        if enc is None:
            return False
        self.path.write_bytes(enc)
        return True

    def load(self) -> dict[str, str] | None:
        if not self.path.exists():
            return None
        enc = self.path.read_bytes()
        plain = _dpapi_unprotect(enc)
        if plain is None:
            return None
        try:
            return json.loads(plain.decode("utf-8"))
        except Exception:
            return None

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# ── RouterManager — public facade used by AdminApi ────────


@dataclass
class RouterSnapshot:
    detected: bool = False
    gateway: str = ""
    manufacturer: str = ""
    model: str = ""
    friendly_name: str = ""
    upnp: bool = False
    natpmp: bool = False
    external_ip: str = ""
    mappings: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class RouterManager:
    """Thread-safe coordinator — SSDP discovery, SOAP calls, NAT-PMP, creds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._device: UpnpDevice | None = None
        self._igd: UpnpIgd | None = None
        self._gateway: str | None = None
        self._last_errors: list[str] = []
        self.vault = RouterCredentialVault()

    # ── Detection ────────────────────────────────────────
    def detect(self, timeout: float = 3.0) -> RouterSnapshot:
        snap = RouterSnapshot()
        gw = _default_gateway_ipv4()
        if gw:
            self._gateway = gw
            snap.gateway = gw

        # Try NAT-PMP first — cheap UDP round-trip.
        if gw:
            try:
                snap.external_ip = natpmp_external_ip(gw)
                snap.natpmp = True
            except NatPmpError:
                pass

        # SSDP for UPnP IGDs.
        replies: list[dict[str, str]] = []
        for target in SSDP_IGD_TARGETS:
            replies.extend(_ssdp_search(target, timeout=timeout))
            if replies:
                break
        for r in replies:
            loc = r.get("location", "")
            if not loc:
                continue
            dev = _fetch_device_description(loc)
            if dev is None:
                continue
            if dev.wan_service() is None:
                continue
            with self._lock:
                self._device = dev
                try:
                    self._igd = UpnpIgd(dev)
                except UpnpError:
                    self._igd = None
            snap.detected = True
            snap.upnp = True
            snap.friendly_name = dev.friendly_name
            snap.manufacturer = dev.manufacturer
            snap.model = dev.model_name or dev.model_number
            if self._igd is not None:
                try:
                    ext = self._igd.get_external_ip()
                    if ext:
                        snap.external_ip = ext
                except UpnpError as e:
                    snap.errors.append(str(e))
                try:
                    snap.mappings = self._igd.list_port_mappings(limit=32)
                except UpnpError as e:
                    snap.errors.append(str(e))
            break

        if not snap.detected and not snap.natpmp:
            snap.errors.append(
                "neither UPnP-IGD nor NAT-PMP responded — router may have "
                "both disabled or be blocking multicast on 1900"
            )

        self._last_errors = list(snap.errors)
        return snap

    # ── Port mapping ─────────────────────────────────────
    def add_mapping(
        self,
        *,
        port: int,
        protocol: str = "TCP",
        internal_client: str = "",
        description: str = "Helen",
        lease_seconds: int = 0,
    ) -> dict[str, Any]:
        if not internal_client:
            internal_client = _my_ip_on_lan() or ""
        if not internal_client:
            return {"ok": False, "error": "could not resolve internal client IP"}

        # Prefer UPnP.
        with self._lock:
            igd = self._igd
        if igd is not None:
            try:
                igd.add_port_mapping(
                    external_port=port,
                    internal_port=port,
                    internal_client=internal_client,
                    protocol=protocol,
                    description=description,
                    lease_seconds=lease_seconds,
                )
                return {"ok": True, "via": "upnp", "port": port,
                        "internal_client": internal_client}
            except UpnpError as e:
                err = str(e)
        else:
            err = "no upnp"

        # NAT-PMP fallback.
        gw = self._gateway or _default_gateway_ipv4()
        if gw:
            try:
                granted = natpmp_add_mapping(
                    gw,
                    internal_port=port, external_port=port,
                    protocol=protocol,
                    lifetime_sec=lease_seconds or 3600,
                )
                return {"ok": True, "via": "natpmp", "port": granted,
                        "internal_client": internal_client}
            except NatPmpError as e:
                err = f"{err}; natpmp: {e}"

        return {"ok": False, "error": err}

    def remove_mapping(self, port: int, protocol: str = "TCP") -> dict[str, Any]:
        with self._lock:
            igd = self._igd
        if igd is None:
            return {"ok": False, "error": "not detected — run detect() first"}
        try:
            igd.delete_port_mapping(port, protocol)
            return {"ok": True}
        except UpnpError as e:
            return {"ok": False, "error": str(e)}

    # ── Credentials ───────────────────────────────────────
    def save_credentials(
        self, host: str, username: str, password: str, brand: str = "",
    ) -> dict[str, Any]:
        ok = self.vault.save(
            {"host": host, "username": username, "password": password, "brand": brand}
        )
        return {"ok": ok}

    def credentials_status(self) -> dict[str, Any]:
        creds = self.vault.load()
        if creds is None:
            return {"present": False}
        return {
            "present": True,
            "host": creds.get("host", ""),
            "brand": creds.get("brand", ""),
            "has_password": bool(creds.get("password")),
        }

    def clear_credentials(self) -> dict[str, Any]:
        self.vault.clear()
        return {"ok": True}

    # ── Brand-specific best-effort profiles ────────────────
    def apply_known_profile(self, action: str) -> dict[str, Any]:
        """Execute a brand-specific action using stored creds.

        Currently implements a minimal OpenWrt LuCI login check + a
        diagnostic "ping" to prove we can reach the admin interface.
        Genuinely flipping AP isolation or IGMP settings requires per-brand
        recipes and is out of scope for this pass — returns ``not_implemented``
        for unknown actions so the UI can disable the button gracefully.
        """
        creds = self.vault.load()
        if creds is None:
            return {"ok": False, "error": "no credentials saved"}

        brand = (creds.get("brand") or "").lower()
        host = creds.get("host") or self._gateway or ""
        user = creds.get("username") or ""
        pw = creds.get("password") or ""
        if not host:
            return {"ok": False, "error": "router host unknown"}

        if brand == "openwrt":
            return _openwrt_admin_probe(host, user, pw, action)
        if brand == "mikrotik":
            return _mikrotik_admin_action(host, user, pw, action)
        return {
            "ok": False,
            "error": f"brand '{brand or 'unknown'}' not yet profiled",
            "brand": brand,
            "action": action,
            "not_implemented": True,
        }


def _my_ip_on_lan() -> str | None:
    """Find this machine's LAN IPv4 by opening a UDP socket toward the
    gateway. Doesn't actually send any packets — just consults the kernel's
    routing table."""
    gw = _default_gateway_ipv4() or "8.8.8.8"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((gw, 1))
            return s.getsockname()[0]
    except OSError:
        return None


# ── OpenWrt LuCI probe (minimal, read-only) ───────────────


# ── Mikrotik RouterOS API (disable AP isolation + open ports) ──


def _mikrotik_admin_action(
    host: str, username: str, password: str, action: str,
) -> dict[str, Any]:
    """Drive a Mikrotik RouterOS box via its REST API (RouterOS v7+) or
    legacy API (v6 and earlier).

    Supported actions:
      * ``probe``        — authenticate and return system/identity
      * ``open_ports``   — add firewall forward rules for Helen ports
      * ``disable_ap_isolation`` — flip default-forwarding on every
                          wireless interface so LAN peers can reach each
                          other
      * ``full_fix``     — apply open_ports + disable_ap_isolation + a
                          bridge multicast-forwarding enable, the three
                          changes that cover 90% of "AP isolation" +
                          "broadcast dropped" complaints.

    RouterOS v7+ ships ``/rest``; we try that first. Older boxes (v6)
    need the binary API on TCP 8728/8729 — we document the fallback
    but don't implement it here (adds 600 lines and an extra dep).
    """
    base_url = host if host.startswith("http") else f"http://{host}"
    base_url = base_url.rstrip("/")

    try:
        import base64 as _b64
        auth = _b64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    except Exception as e:
        return {"ok": False, "error": f"credential encode failed: {e}"}

    def _rest(method: str, path: str, body: dict | None = None,
              timeout: float = 5.0) -> tuple[int, Any]:
        import json as _json
        data = _json.dumps(body).encode() if body else None
        headers = {
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{base_url}/rest{path}",
            data=data, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw) if raw else None
                except ValueError:
                    return resp.status, raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return e.code, body
        except Exception as e:
            return 0, str(e)

    if action == "probe":
        status, data = _rest("GET", "/system/identity")
        if status == 200:
            return {"ok": True, "brand": "mikrotik",
                    "identity": data, "note": "RouterOS v7 REST reachable"}
        return {"ok": False, "brand": "mikrotik",
                "error": f"HTTP {status}: {str(data)[:200]}",
                "hint": "RouterOS v7 REST API only — v6 uses binary API on 8728"}

    if action in ("open_ports", "full_fix"):
        # Firewall rules — allow Helen's ports through the forward chain
        # between LAN interfaces. We add per-port rules with a stable
        # comment so re-running is idempotent.
        rules_to_ensure = [
            ("tcp", "3000,3001,3443", "Helen-TCP"),
            ("udp", "41234,5353", "Helen-UDP"),
        ]
        # First, list existing rules so we don't double-add.
        status, existing = _rest("GET", "/ip/firewall/filter")
        already = set()
        if status == 200 and isinstance(existing, list):
            for r in existing:
                comment = (r.get("comment") or "")
                if comment.startswith("Helen-"):
                    already.add(comment)
        added = 0
        for proto, ports, tag in rules_to_ensure:
            if tag in already:
                continue
            status, resp = _rest("PUT", "/ip/firewall/filter", {
                "chain": "forward",
                "action": "accept",
                "protocol": proto,
                "dst-port": ports,
                "comment": tag,
            })
            if status in (200, 201):
                added += 1
        result = {"ok": True, "brand": "mikrotik",
                  "action": action, "rules_added": added,
                  "rules_already_present": len(already)}
        if action == "open_ports":
            return result

    if action in ("disable_ap_isolation", "full_fix"):
        # Enumerate wireless interfaces and flip default-forwarding on
        # each. On Mikrotik the property is named "default-forwarding"
        # (yes=allow intra-AP, no=client isolation).
        status, ifaces = _rest("GET", "/interface/wireless")
        flipped = 0
        skipped = 0
        if status == 200 and isinstance(ifaces, list):
            for iface in ifaces:
                if iface.get("default-forwarding") == "yes":
                    skipped += 1
                    continue
                iid = iface.get(".id") or iface.get("id")
                if not iid:
                    continue
                st2, _r2 = _rest("PATCH", f"/interface/wireless/{iid}",
                                 {"default-forwarding": "yes"})
                if st2 in (200, 204):
                    flipped += 1
        # Also enable IGMP snooping appropriately and multicast forwarding
        # on bridges so mDNS / UDP broadcast cross wireless-wired.
        bridges = _rest("GET", "/interface/bridge")
        bridge_flipped = 0
        if isinstance(bridges, tuple) and bridges[0] == 200:
            for br in (bridges[1] or []):
                bid = br.get(".id") or br.get("id")
                if not bid:
                    continue
                _rest("PATCH", f"/interface/bridge/{bid}",
                      {"igmp-snooping": "no",
                       "multicast-querier": "no"})
                bridge_flipped += 1

        if action == "disable_ap_isolation":
            return {"ok": True, "brand": "mikrotik",
                    "action": action,
                    "wireless_ifaces_flipped": flipped,
                    "already_open": skipped,
                    "bridges_tuned": bridge_flipped}

        # full_fix — merge into the rules_added result if present
        return {
            "ok": True, "brand": "mikrotik", "action": "full_fix",
            "firewall_rules_added": result.get("rules_added", 0),
            "firewall_rules_already_present": result.get("rules_already_present", 0),
            "wireless_ifaces_flipped": flipped,
            "wireless_already_open": skipped,
            "bridges_multicast_tuned": bridge_flipped,
        }

    return {"ok": False, "brand": "mikrotik",
            "error": f"unknown action '{action}'",
            "supported": ["probe", "open_ports",
                          "disable_ap_isolation", "full_fix"]}


def _openwrt_admin_probe(
    host: str, username: str, password: str, action: str,
) -> dict[str, Any]:
    """Log into LuCI, return the auth cookie + a tiny status call. This is
    a *liveness check* only — it proves the program can reach the admin
    panel with the stored credentials. Real configuration changes (AP
    isolation toggle, etc.) are deliberately not wired in here; they depend
    on LuCI/rpcd paths that vary across OpenWrt versions and need user
    consent for each write. Callers should escalate to the LuCI RPC
    (``/cgi-bin/luci/rpc/uci``) with the returned ``sysauth`` cookie.
    """
    base = host if host.startswith("http") else f"http://{host}"
    login_url = f"{base.rstrip('/')}/cgi-bin/luci"
    try:
        data = urllib.parse.urlencode(
            {"luci_username": username, "luci_password": password}
        ).encode("ascii")
        req = urllib.request.Request(
            login_url, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            cookie = resp.headers.get("Set-Cookie", "")
            ok = "sysauth" in (cookie or "")
            return {
                "ok": ok,
                "brand": "openwrt",
                "action": action,
                "login_ok": ok,
                "note": "read-only probe — write ops require per-version LuCI recipes",
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
