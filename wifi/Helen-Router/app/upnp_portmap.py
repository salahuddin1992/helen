"""
UPnP IGD port-mapping client.

Once :func:`external_routers.discover_ssdp` returns a router with a
``upnp_url`` (the device-description XML), this module can fetch that
XML, locate the WANIPConnection / WANPPPConnection control URL, and
ask the router to forward an external port to a Helen-Server.

Used by ``app/main.py`` when the deployment spans multiple LANs that
sit behind a NAT router. The admin sets ``HELEN_ROUTER_AUTO_PORTMAP=1``
and the router does the rest at startup.

This is a tiny pure-Python implementation that targets the most common
IGD profiles. It avoids the ``upnpclient``/``miniupnpc`` deps so it
ships clean inside the PyInstaller bundle.
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx


_IGD_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


@dataclass
class IGDService:
    base_url: str          # http://192.168.1.1:5000
    control_url: str       # /WANIPConnection_ctrl
    service_type: str      # urn:schemas-...
    external_ip: Optional[str] = None


def _abs_url(device_url: str, path: str) -> str:
    """Resolve a relative control URL against the device URL."""
    if path.startswith("http"):
        return path
    parsed = urlparse(device_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return urljoin(base, path)


async def fetch_igd_service(device_url: str,
                             timeout_sec: float = 4.0
                             ) -> Optional[IGDService]:
    """Fetch the device-description XML and locate a WAN service.

    Returns None if the device isn't an IGD or the XML can't be
    parsed. Robust against broken-but-common XML by using regex
    rather than xml.etree.
    """
    async with httpx.AsyncClient(timeout=timeout_sec) as c:
        try:
            r = await c.get(device_url)
            if r.status_code != 200:
                return None
            xml = r.text
        except Exception:
            return None

    # Walk the service list — IGD nests WANConnectionDevice inside
    # WANDevice inside the root device.
    for stype in _IGD_SERVICE_TYPES:
        # Capture the <service> block whose <serviceType> matches.
        block_re = re.compile(
            r"<service>\s*([\s\S]*?)</service>", re.IGNORECASE,
        )
        for m in block_re.finditer(xml):
            block = m.group(1)
            if stype not in block:
                continue
            ctrl = re.search(
                r"<controlURL>\s*([^<]+)\s*</controlURL>",
                block, re.IGNORECASE,
            )
            if not ctrl:
                continue
            return IGDService(
                base_url=device_url,
                control_url=_abs_url(device_url, ctrl.group(1).strip()),
                service_type=stype,
            )
    return None


def _build_soap(action: str, service_type: str, body_inner: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        f'<u:{action} xmlns:u="{service_type}">'
        f'{body_inner}'
        f'</u:{action}>'
        '</s:Body></s:Envelope>'
    )


async def soap_call(svc: IGDService, action: str,
                     body_inner: str = "",
                     timeout_sec: float = 4.0) -> Optional[str]:
    """Issue a SOAP action against the IGD's control URL. Returns the
    response body on success, None on any HTTP / parse failure."""
    headers = {
        "SOAPAction": f'"{svc.service_type}#{action}"',
        "Content-Type": "text/xml; charset=utf-8",
    }
    body = _build_soap(action, svc.service_type, body_inner)
    async with httpx.AsyncClient(timeout=timeout_sec) as c:
        try:
            r = await c.post(svc.control_url, headers=headers,
                             content=body)
            if r.status_code != 200:
                return None
            return r.text
        except Exception:
            return None


async def get_external_ip(svc: IGDService) -> Optional[str]:
    resp = await soap_call(svc, "GetExternalIPAddress")
    if not resp:
        return None
    m = re.search(r"<NewExternalIPAddress>\s*([^<]+)\s*</NewExternalIPAddress>",
                  resp, re.IGNORECASE)
    if m:
        ip = m.group(1).strip()
        svc.external_ip = ip
        return ip
    return None


async def add_port_mapping(
    svc: IGDService,
    *,
    external_port: int,
    internal_port: int,
    internal_client: str,
    description: str = "Helen",
    protocol: str = "TCP",
    lease_duration: int = 0,   # 0 = permanent on most routers
) -> bool:
    body = (
        "<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{external_port}</NewExternalPort>"
        f"<NewProtocol>{protocol.upper()}</NewProtocol>"
        f"<NewInternalPort>{internal_port}</NewInternalPort>"
        f"<NewInternalClient>{internal_client}</NewInternalClient>"
        "<NewEnabled>1</NewEnabled>"
        f"<NewPortMappingDescription>{description}</NewPortMappingDescription>"
        f"<NewLeaseDuration>{lease_duration}</NewLeaseDuration>"
    )
    resp = await soap_call(svc, "AddPortMapping", body)
    return resp is not None and "<u:AddPortMappingResponse" in resp


async def delete_port_mapping(
    svc: IGDService,
    *,
    external_port: int,
    protocol: str = "TCP",
) -> bool:
    body = (
        "<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{external_port}</NewExternalPort>"
        f"<NewProtocol>{protocol.upper()}</NewProtocol>"
    )
    resp = await soap_call(svc, "DeletePortMapping", body)
    return resp is not None and "<u:DeletePortMappingResponse" in resp


async def auto_map_for_helen_server(
    upnp_url: str,
    *,
    helen_internal_ip: Optional[str] = None,
    external_port: int = 3000,
    internal_port: int = 3000,
) -> tuple[bool, Optional[str]]:
    """One-call helper: discover the IGD service and forward the
    Helen-Server port through it. Returns ``(ok, external_ip)``.

    ``helen_internal_ip`` defaults to the host's primary LAN IP.
    """
    if helen_internal_ip is None:
        helen_internal_ip = _local_lan_ip() or "127.0.0.1"

    svc = await fetch_igd_service(upnp_url)
    if not svc:
        return False, None
    external_ip = await get_external_ip(svc)
    ok = await add_port_mapping(
        svc,
        external_port=external_port,
        internal_port=internal_port,
        internal_client=helen_internal_ip,
        description="Helen-Server",
    )
    return ok, external_ip


def _local_lan_ip() -> Optional[str]:
    """A best-guess primary IPv4 address of this host. Closes the
    probing socket on every path (success, OSError, anything else)
    so we don't leak a UDP socket on hosts where the connect() fails."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return None
