"""
Camera discovery — enumerate local capture devices and scan the LAN
for ONVIF-compliant IP cameras.

Two entry points:

    list_local_cameras() -> list[dict]
        Shells out to `ffmpeg -list_devices` (Windows: dshow) or globs
        `/dev/video*` (Linux). Returns a list of {id, label, platform}.

    async discover_onvif(timeout=4.0) -> list[dict]
        Sends an ONVIF WS-Discovery probe via UDP multicast 239.255.255.250
        and parses the XML responses. Returns a list of
        {endpoint, name, xaddrs, suggested_rtsp}.

Both helpers NEVER raise on missing dependencies — they return [] and
log a debug line. The admin UI surfaces "0 devices found" so the user
can fall back to manual entry.
"""

from __future__ import annotations

import asyncio
import glob
import re
import socket
import subprocess
import sys
import uuid
from typing import Any

from app.core.gpu_detect import probe as gpu_probe
from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Local USB / capture-card enumeration ────────────────────

def list_local_cameras() -> list[dict[str, Any]]:
    """
    Return every video capture device visible to this machine.

    Windows — parses `ffmpeg -list_devices true -f dshow -i dummy`.
    Linux   — globs /dev/video* and reads each's v4l2 name.
    macOS   — parses `ffmpeg -f avfoundation -list_devices true -i ""`.
    """
    try:
        if sys.platform.startswith("win"):
            return _list_dshow()
        if sys.platform == "darwin":
            return _list_avfoundation()
        return _list_v4l2()
    except Exception as exc:
        logger.warning("camera_enum_failed", error=str(exc))
        return []


def _run_ffmpeg_devices(extra_args: list[str]) -> str:
    """Invoke ffmpeg with device-listing flags; return combined output."""
    ff = gpu_probe().ffmpeg_path
    if not ff:
        return ""
    cmd = [ff, "-hide_banner", *extra_args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=6.0, check=False,
            errors="replace",
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        logger.debug("ffmpeg_device_probe_failed", error=str(exc))
        return ""


def _list_dshow() -> list[dict[str, Any]]:
    out = _run_ffmpeg_devices(["-list_devices", "true", "-f", "dshow", "-i", "dummy"])
    if not out:
        return []
    # ffmpeg 5.x+ drops the section headers and emits per-line:
    #   [dshow @ 0x...] "Device Name" (video)
    #   [dshow @ 0x...]   Alternative name "@device_pnp_..."
    # Older builds still print "DirectShow video devices" banners.
    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in out.splitlines():
        if "Alternative name" in line:
            continue
        if not line.lstrip().startswith("[dshow"):
            continue
        if "(video)" not in line:
            continue
        m = re.search(r'"([^"]+)"', line)
        if not m:
            continue
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        devices.append({
            "id": name,
            "label": name,
            "platform": "dshow",
            "suggested_url": name,
        })
    return devices


def _list_v4l2() -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for path in sorted(glob.glob("/dev/video*")):
        label = path
        try:
            with open(f"/sys/class/video4linux/{path.split('/')[-1]}/name") as fh:
                label = fh.read().strip() or path
        except OSError:
            pass
        devices.append({
            "id": path,
            "label": label,
            "platform": "v4l2",
            "suggested_url": path,
        })
    return devices


def _list_avfoundation() -> list[dict[str, Any]]:
    out = _run_ffmpeg_devices(["-f", "avfoundation", "-list_devices", "true", "-i", ""])
    devices: list[dict[str, Any]] = []
    in_video = False
    for line in out.splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
            continue
        if "AVFoundation audio devices" in line:
            in_video = False
            continue
        if not in_video:
            continue
        # Format: [AVFoundation indev @ 0x...] [0] FaceTime HD Camera
        m = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if m:
            idx = m.group(1)
            name = m.group(2).strip()
            devices.append({
                "id": idx,
                "label": name,
                "platform": "avfoundation",
                "suggested_url": idx,
            })
    return devices


# ── ONVIF WS-Discovery ──────────────────────────────────────

_ONVIF_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{msg_id}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""


async def discover_onvif(timeout: float = 4.0) -> list[dict[str, Any]]:
    """
    Send a WS-Discovery Probe and collect responses for ONVIF NVTs.

    Returns a deduplicated list of
        {endpoint, name, xaddrs: [...], suggested_rtsp}

    ONVIF devices respond with ProbeMatch XML that contains XAddrs
    (SOAP endpoints). The RTSP URL is typically available via the
    ONVIF GetStreamUri call but constructing a sensible default here
    saves the operator another round-trip — most cameras honor
    rtsp://<ip>:554/onvif1 or similar. Operators can edit before saving.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _probe_onvif_sync, timeout)


def _probe_onvif_sync(timeout: float) -> list[dict[str, Any]]:
    group = "239.255.255.250"
    port = 3702

    msg = _ONVIF_PROBE.format(msg_id=str(uuid.uuid4())).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.bind(("", 0))
        sock.sendto(msg, (group, port))

        seen_xaddrs: set[str] = set()
        results: list[dict[str, Any]] = []
        deadline = asyncio.get_event_loop().time() + timeout if False else None  # unused
        import time as _t
        end = _t.monotonic() + timeout
        while _t.monotonic() < end:
            try:
                sock.settimeout(max(0.1, end - _t.monotonic()))
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                break
            except OSError:
                break
            text = data.decode("utf-8", errors="replace")
            # crude parse — pulls XAddrs + Types from the ProbeMatch envelope
            xaddrs = _extract_tag(text, "XAddrs")
            addrs = [u for u in xaddrs.split() if u.startswith(("http://", "https://"))]
            for x in addrs:
                if x in seen_xaddrs:
                    continue
                seen_xaddrs.add(x)
                ip = _host_from_url(x) or addr[0]
                results.append({
                    "endpoint": x,
                    "name": f"ONVIF @ {ip}",
                    "ip": ip,
                    "suggested_rtsp": f"rtsp://{ip}:554/onvif1",
                })
        return results
    except Exception as exc:
        logger.warning("onvif_probe_failed", error=str(exc))
        return []
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _extract_tag(xml: str, tag: str) -> str:
    m = re.search(rf"<[^>]*?:{tag}[^>]*?>(.*?)</[^>]*?:{tag}>", xml, re.DOTALL)
    return (m.group(1) if m else "").strip()


def _host_from_url(url: str) -> str | None:
    m = re.match(r"https?://([^:/]+)", url)
    return m.group(1) if m else None


__all__ = ["list_local_cameras", "discover_onvif"]
