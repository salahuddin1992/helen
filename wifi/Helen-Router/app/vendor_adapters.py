"""
Vendor-specific adapters for popular SOHO / enterprise routers.

When ``external_routers.discover_all()`` finds a router, Helen tries
the matching vendor adapter for **read-only** queries (uptime,
neighbours, link state). Adapters that require credentials (RouterOS
REST, UniFi REST) are only invoked when the admin sets vendor
credentials in env, otherwise we stop at the public ``/`` page sniff.

Supported (best-effort, no-internet):

  * Mikrotik RouterOS (REST API on 80/443/HTTP, fingerprint via Server
    header "Mikrotik HttpProxy")
  * Ubiquiti UniFi (controller / dream-machine REST)
  * OpenWrt LuCI (HTTP page sniff + UBUS RPC if creds given)
  * pfSense (page sniff)
  * Cisco IOS XE (page sniff via NX-API or RESTCONF if creds given)
  * Generic UPnP-only (fallback — uses upnp_portmap)

Each adapter implements the same shape::

    async def fingerprint(http: httpx.AsyncClient, ip: str) -> dict | None
    async def fetch_status(http: httpx.AsyncClient, ip: str,
                           creds: dict | None = None) -> dict | None
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import httpx


# ── Generic helpers ─────────────────────────────────────────────────


async def _try_get(http: httpx.AsyncClient, url: str,
                    timeout: float = 2.0) -> Optional[httpx.Response]:
    try:
        return await http.get(url, timeout=timeout, follow_redirects=True)
    except Exception:
        return None


# ── Mikrotik RouterOS ───────────────────────────────────────────────


async def mikrotik_fingerprint(http: httpx.AsyncClient,
                                ip: str) -> Optional[dict]:
    for scheme in ("http", "https"):
        r = await _try_get(http, f"{scheme}://{ip}/")
        if r is None:
            continue
        server = r.headers.get("server", "")
        body = r.text[:4096].lower() if r.status_code < 500 else ""
        if ("mikrotik" in server.lower()
                or "mikrotik" in body
                or "routeros" in body):
            return {
                "vendor": "Mikrotik",
                "fingerprint": "RouterOS",
                "scheme": scheme,
                "server_header": server,
            }
    return None


async def mikrotik_fetch_status(
    http: httpx.AsyncClient, ip: str,
    creds: Optional[dict] = None,
) -> Optional[dict]:
    """Use the RouterOS REST API (RouterOS 7.x)."""
    if not creds or "user" not in creds:
        return None
    user = creds["user"]
    password = creds.get("password", "")
    auth = (user, password)
    base = creds.get("base") or f"http://{ip}/rest"

    endpoints = ["system/resource", "system/identity", "interface"]
    out: dict = {"vendor": "Mikrotik"}
    for ep in endpoints:
        r = await _try_get(http, f"{base}/{ep}")
        if r is None or r.status_code != 200:
            continue
        try:
            out[ep] = r.json()
        except Exception:
            continue
    return out if len(out) > 1 else None


# ── Ubiquiti UniFi ──────────────────────────────────────────────────


async def unifi_fingerprint(http: httpx.AsyncClient,
                             ip: str) -> Optional[dict]:
    # UniFi controllers run on 8443 (legacy) or 443 (UDM)
    for url in (f"https://{ip}:8443/manage", f"https://{ip}/"):
        r = await _try_get(http, url)
        if r is None:
            continue
        body = r.text[:8192].lower() if r.status_code < 500 else ""
        if "unifi" in body or "ubiquiti" in body:
            return {
                "vendor": "Ubiquiti",
                "fingerprint": "UniFi-Controller",
                "url": url,
            }
    return None


# ── OpenWrt / LuCI ──────────────────────────────────────────────────


async def openwrt_fingerprint(http: httpx.AsyncClient,
                                ip: str) -> Optional[dict]:
    for url in (f"http://{ip}/", f"http://{ip}/cgi-bin/luci"):
        r = await _try_get(http, url)
        if r is None:
            continue
        body = r.text[:8192].lower() if r.status_code < 500 else ""
        if ("openwrt" in body or "lede" in body or "luci" in body):
            return {
                "vendor": "OpenWrt",
                "fingerprint": "LuCI",
                "url": url,
            }
    return None


# ── pfSense ─────────────────────────────────────────────────────────


async def pfsense_fingerprint(http: httpx.AsyncClient,
                                ip: str) -> Optional[dict]:
    for url in (f"https://{ip}/", f"http://{ip}/"):
        r = await _try_get(http, url)
        if r is None:
            continue
        body = r.text[:8192].lower() if r.status_code < 500 else ""
        if "pfsense" in body or "netgate" in body:
            return {
                "vendor": "pfSense",
                "fingerprint": "pfSense-WebGUI",
                "url": url,
            }
    return None


# ── TP-Link / Generic SOHO ──────────────────────────────────────────


async def tplink_fingerprint(http: httpx.AsyncClient,
                                ip: str) -> Optional[dict]:
    r = await _try_get(http, f"http://{ip}/")
    if r is None:
        return None
    body = r.text[:8192].lower() if r.status_code < 500 else ""
    if "tp-link" in body or "tplink" in body or "archer" in body:
        return {"vendor": "TP-Link", "fingerprint": "WebGUI"}
    return None


# ── Asus ────────────────────────────────────────────────────────────


async def asus_fingerprint(http: httpx.AsyncClient,
                            ip: str) -> Optional[dict]:
    r = await _try_get(http, f"http://{ip}/")
    if r is None:
        return None
    body = r.text[:8192].lower() if r.status_code < 500 else ""
    if "asus" in body or "asuswrt" in body:
        return {"vendor": "Asus", "fingerprint": "ASUSWRT"}
    return None


# ── Generic / Cisco / Netgear / D-Link sniff ────────────────────────


_VENDOR_KEYWORDS = {
    "Cisco":   ("cisco", "linksys ea", "ios xe"),
    "Netgear": ("netgear", "nighthawk"),
    "D-Link":  ("d-link", "dlink", "dir-"),
    "Huawei":  ("huawei",),
    "Aruba":   ("aruba networks", "instant on"),
    "Sophos":  ("sophos",),
    "Fortinet": ("fortinet", "fortigate"),
    "Sonicwall": ("sonicwall",),
}


async def generic_fingerprint(http: httpx.AsyncClient,
                                ip: str) -> Optional[dict]:
    for scheme in ("http", "https"):
        r = await _try_get(http, f"{scheme}://{ip}/")
        if r is None:
            continue
        body = (r.text or "")[:8192].lower()
        title_match = re.search(r"<title>([^<]{1,80})</title>",
                                  body, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else ""
        for vendor, keywords in _VENDOR_KEYWORDS.items():
            if any(k in body for k in keywords):
                return {"vendor": vendor,
                        "fingerprint": title or "WebGUI",
                        "scheme": scheme}
    return None


# ── Top-level entry ─────────────────────────────────────────────────


_FINGERPRINTERS = (
    mikrotik_fingerprint,
    unifi_fingerprint,
    openwrt_fingerprint,
    pfsense_fingerprint,
    tplink_fingerprint,
    asus_fingerprint,
    generic_fingerprint,
)


async def identify_vendor(ip: str,
                            timeout_sec: float = 5.0
                            ) -> Optional[dict]:
    """Run every fingerprinter against ``ip`` in parallel; return the
    first non-None result. Order matches the tuple above so
    Mikrotik/UniFi/OpenWrt vendor-specific signatures win over generic
    sniffs.
    """
    async with httpx.AsyncClient(
        timeout=timeout_sec, verify=False, follow_redirects=True,
    ) as http:
        # First try each in order — first hit wins. We don't run them
        # concurrently because a single router answers identically on
        # every fingerprinter and a pile of parallel requests is a
        # bigger blast radius than serial sniffs.
        for fn in _FINGERPRINTERS:
            try:
                result = await fn(http, ip)
                if result:
                    return result
            except Exception:
                continue
    return None
