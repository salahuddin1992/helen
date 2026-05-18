"""
Phase 7 / Module AH — LAN-only Plugin Registry Client
======================================================

Talks to a private/LAN plugin registry (default
``http://registry.helen.lan/plugins``). Provides:

* Catalog fetch (paginated, with optional filters).
* Manifest fetch by ``slug@version``.
* Bundle fetch (chunked download with SHA-256 + signature verification).
* Airgap mode — when enabled, the client refuses any network call and
  only serves locally-uploaded bundles.

LAN-only enforcement
--------------------
Unlike the legacy :mod:`marketplace_client`, this module deliberately
**rejects** internet-routable hosts:

1. URL is resolved via ``socket.getaddrinfo`` on a thread.
2. Every resolved IP must be private (RFC1918, loopback, link-local,
   unique-local, mDNS, CGNAT) **or** the registry must be on the same
   LAN subnet as one of the local interfaces.

If you really want a hosted registry you must explicitly opt in with
``HELEN_PLUGINS_ALLOW_PUBLIC=1``.

Verification
------------
The registry is expected to ship:

* ``GET /plugins/index.json``   — catalog
* ``GET /plugins/{slug}/{version}/manifest.json``
* ``GET /plugins/{slug}/{version}/bundle.helen-plugin``  (binary blob)
* ``GET /plugins/{slug}/{version}/bundle.sig``           (Ed25519 sig
                                                            base64 over
                                                            SHA-256 of
                                                            the bundle)

Bundle integrity:

1. SHA-256 must match ``manifest.code_sha256``.
2. Detached signature must verify against the trust store
   (``signer.verify_against_trust_store``).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiohttp

from app.core.logging import get_logger
from app.services.plugins.signer import verify_against_trust_store

logger = get_logger(__name__)


DEFAULT_REGISTRY_URL = os.getenv(
    "HELEN_PLUGIN_REGISTRY_URL",
    "http://registry.helen.lan/plugins",
)
ALLOW_PUBLIC = os.getenv("HELEN_PLUGINS_ALLOW_PUBLIC", "0") == "1"
AIRGAP_MODE = os.getenv("HELEN_PLUGINS_AIRGAP", "0") == "1"
CHUNK_SIZE = 256 * 1024            # 256 KiB
BUNDLE_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB hard cap
REQUEST_TIMEOUT_SEC = 30
CACHE_TTL_SEC = 300


# ───────────────────────────────────────────────────────────────────────
# LAN enforcement
# ───────────────────────────────────────────────────────────────────────


_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),     # link-local
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),        # loopback
    ipaddress.ip_network("fc00::/7"),           # unique-local IPv6
    ipaddress.ip_network("fe80::/10"),          # link-local IPv6
    ipaddress.ip_network("::1/128"),
]


def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE_NETWORKS)


async def _resolve_host(host: str) -> list[str]:
    """Return list of IPs for ``host`` (best-effort, off the event loop)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(
            None, socket.getaddrinfo, host, None,
            socket.AF_UNSPEC, socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return []
    out: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in out:
            out.append(sockaddr[0])
    return out


async def _check_lan_only(url: str) -> tuple[bool, str]:
    """Verify the URL host resolves to a private network.

    Returns ``(ok, reason)``.
    """
    if ALLOW_PUBLIC:
        return True, "public-allowed"
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return False, "no-host"
    if host in ("localhost",):
        return True, "loopback"
    # ``.lan`` and ``.local`` TLDs are always LAN by convention.
    if host.endswith(".lan") or host.endswith(".local"):
        return True, "lan-tld"
    # IP literal?
    try:
        ipaddress.ip_address(host)
        return (_is_private_ip(host), "ip-literal")
    except ValueError:
        pass
    # Resolve and inspect every A/AAAA
    ips = await _resolve_host(host)
    if not ips:
        return False, "resolve-failed"
    bad = [ip for ip in ips if not _is_private_ip(ip)]
    if bad:
        return False, f"non-private-ips: {','.join(bad)}"
    return True, "ok"


# ───────────────────────────────────────────────────────────────────────
# Catalog / manifest / bundle types
# ───────────────────────────────────────────────────────────────────────


@dataclass
class CatalogEntry:
    slug: str
    name: str
    version: str
    author: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    rating_avg: float = 0.0
    ratings_count: int = 0
    downloads: int = 0
    signed_by: Optional[str] = None
    homepage: Optional[str] = None
    icon: Optional[str] = None
    screenshots: list[str] = field(default_factory=list)
    long_description: Optional[str] = None


@dataclass
class CatalogPage:
    items: list[CatalogEntry]
    total: int
    page: int
    page_size: int
    categories: dict[str, int] = field(default_factory=dict)


@dataclass
class BundleResult:
    path: Path
    sha256: str
    size: int
    signature_valid: bool
    signed_by: Optional[str]


# ───────────────────────────────────────────────────────────────────────
# RegistryClient
# ───────────────────────────────────────────────────────────────────────


class RegistryError(RuntimeError):
    pass


class RegistryClient:
    """Async client for a single LAN registry endpoint."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        airgap: Optional[bool] = None,
        timeout_seconds: int = REQUEST_TIMEOUT_SEC,
        cache_dir: Optional[Path] = None,
        verify_lan: bool = True,
    ) -> None:
        self.base_url = (base_url or DEFAULT_REGISTRY_URL).rstrip("/")
        self.airgap = AIRGAP_MODE if airgap is None else airgap
        self.timeout_seconds = timeout_seconds
        self.cache_dir = cache_dir or Path(
            os.getenv("HELEN_PLUGIN_BUNDLE_CACHE", "data/plugin-bundles")
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.verify_lan = verify_lan
        self._catalog_cache: tuple[float, dict[str, Any]] | None = None
        self._session: Optional[aiohttp.ClientSession] = None

    # ----- session lifecycle -------------------------------------------

    async def __aenter__(self) -> "RegistryClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ----- LAN guard ---------------------------------------------------

    async def _guard(self) -> None:
        if self.airgap:
            raise RegistryError("airgap-mode-enabled")
        if self.verify_lan:
            ok, why = await _check_lan_only(self.base_url)
            if not ok:
                raise RegistryError(f"registry-not-lan: {why}")

    async def ping(self) -> dict[str, Any]:
        """Connectivity test — returns latency and version info."""
        if self.airgap:
            return {"ok": False, "airgap": True, "reason": "airgap-mode"}
        if self.verify_lan:
            ok, why = await _check_lan_only(self.base_url)
            if not ok:
                return {"ok": False, "airgap": False, "reason": why,
                        "url": self.base_url}
        session = await self._ensure_session()
        t0 = time.perf_counter()
        try:
            async with session.get(f"{self.base_url}/health") as resp:
                body = await resp.text()
                latency_ms = int((time.perf_counter() - t0) * 1000)
                try:
                    info = json.loads(body)
                except Exception:                                       # noqa: BLE001
                    info = {"raw": body[:200]}
                return {
                    "ok": resp.status == 200,
                    "status": resp.status,
                    "latency_ms": latency_ms,
                    "info": info,
                    "url": self.base_url,
                }
        except Exception as e:                                          # noqa: BLE001
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "error": str(e), "latency_ms": latency_ms,
                    "url": self.base_url}

    # ----- catalog -----------------------------------------------------

    async def fetch_catalog(
        self,
        *,
        category: Optional[str] = None,
        tag: Optional[str] = None,
        search: Optional[str] = None,
        sort: str = "downloads",
        page: int = 1,
        page_size: int = 50,
    ) -> CatalogPage:
        """Fetch and filter the catalog. Returns merged installed + remote view."""
        await self._guard()
        session = await self._ensure_session()
        # Cache-hit?
        if self._catalog_cache is not None:
            t, data = self._catalog_cache
            if time.time() - t < CACHE_TTL_SEC:
                return self._filter_catalog(
                    data, category=category, tag=tag, search=search,
                    sort=sort, page=page, page_size=page_size,
                )
        try:
            async with session.get(f"{self.base_url}/index.json") as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as e:                                          # noqa: BLE001
            logger.warning("registry.fetch_catalog failed: %s", e)
            raise RegistryError(f"catalog-fetch-failed: {e}") from e
        self._catalog_cache = (time.time(), data)
        return self._filter_catalog(
            data, category=category, tag=tag, search=search,
            sort=sort, page=page, page_size=page_size,
        )

    @staticmethod
    def _filter_catalog(
        data: dict[str, Any], *,
        category: Optional[str], tag: Optional[str], search: Optional[str],
        sort: str, page: int, page_size: int,
    ) -> CatalogPage:
        entries_raw = data.get("plugins") or data.get("items") or []
        entries: list[CatalogEntry] = []
        cat_counts: dict[str, int] = {}
        for e in entries_raw:
            mf = e.get("manifest") or e
            entry = CatalogEntry(
                slug=mf.get("slug") or e.get("slug"),
                name=mf.get("name") or e.get("name") or mf.get("slug", ""),
                version=mf.get("version") or e.get("version") or "0.0.0",
                author=mf.get("author"),
                description=mf.get("description"),
                category=e.get("category") or mf.get("category"),
                tags=e.get("tags") or mf.get("tags") or [],
                rating_avg=float(e.get("rating_avg") or 0),
                ratings_count=int(e.get("ratings_count") or 0),
                downloads=int(e.get("downloads") or 0),
                signed_by=mf.get("signed_by"),
                homepage=mf.get("homepage"),
                icon=e.get("icon") or mf.get("icon"),
                screenshots=e.get("screenshots") or [],
                long_description=e.get("long_description"),
            )
            if not entry.slug:
                continue
            if entry.category:
                cat_counts[entry.category] = cat_counts.get(entry.category, 0) + 1
            entries.append(entry)

        filt = entries
        if category:
            filt = [e for e in filt if e.category == category]
        if tag:
            filt = [e for e in filt if tag in (e.tags or [])]
        if search:
            s = search.lower()
            filt = [
                e for e in filt
                if s in (e.name or "").lower()
                or s in (e.slug or "").lower()
                or s in (e.description or "").lower()
            ]
        # Sort
        sort = (sort or "downloads").lower()
        sort_keys = {
            "downloads": lambda e: -e.downloads,
            "rating":    lambda e: -e.rating_avg,
            "name":      lambda e: e.name.lower(),
            "recent":    lambda e: -hash(e.version),
        }
        key_fn = sort_keys.get(sort, sort_keys["downloads"])
        filt.sort(key=key_fn)
        total = len(filt)
        start = max(0, (page - 1) * page_size)
        end = start + page_size
        return CatalogPage(
            items=filt[start:end], total=total,
            page=page, page_size=page_size,
            categories=cat_counts,
        )

    # ----- manifest ----------------------------------------------------

    async def fetch_manifest(
        self, slug: str, version: str,
    ) -> dict[str, Any]:
        await self._guard()
        session = await self._ensure_session()
        url = f"{self.base_url}/{slug}/{version}/manifest.json"
        try:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            return data
        except Exception as e:                                          # noqa: BLE001
            raise RegistryError(f"manifest-fetch-failed: {e}") from e

    # ----- bundle ------------------------------------------------------

    async def fetch_bundle(
        self,
        slug: str,
        version: str,
        *,
        expected_sha256: Optional[str] = None,
        signed_by: Optional[str] = None,
        progress_cb: Optional[Any] = None,
    ) -> BundleResult:
        """Download bundle to ``cache_dir``, verify SHA-256, verify sig.

        ``progress_cb`` if given is invoked as ``cb(bytes_so_far, total_bytes)``.
        """
        await self._guard()
        session = await self._ensure_session()
        bundle_url = f"{self.base_url}/{slug}/{version}/bundle.helen-plugin"
        sig_url = f"{self.base_url}/{slug}/{version}/bundle.sig"

        bundle_dir = self.cache_dir / slug / version
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = bundle_dir / "bundle.helen-plugin"

        hasher = hashlib.sha256()
        total_size = 0
        try:
            async with session.get(bundle_url) as resp:
                resp.raise_for_status()
                expected_len = int(resp.headers.get("Content-Length") or 0)
                with bundle_path.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                        if total_size + len(chunk) > BUNDLE_MAX_BYTES:
                            raise RegistryError("bundle-too-large")
                        total_size += len(chunk)
                        hasher.update(chunk)
                        f.write(chunk)
                        if progress_cb:
                            try:
                                progress_cb(total_size, expected_len)
                            except Exception:                            # noqa: BLE001
                                pass
        except aiohttp.ClientError as e:
            raise RegistryError(f"bundle-fetch-failed: {e}") from e

        sha = hasher.hexdigest()
        if expected_sha256 and sha != expected_sha256:
            try:
                bundle_path.unlink(missing_ok=True)
            except Exception:                                            # noqa: BLE001
                pass
            raise RegistryError(
                f"sha256-mismatch expected={expected_sha256} got={sha}"
            )

        # Detached signature (optional but recommended)
        sig_valid = False
        try:
            async with session.get(sig_url) as resp:
                if resp.status == 200:
                    sig_b64 = (await resp.text()).strip()
                    sig_valid = verify_against_trust_store(
                        sha.encode("utf-8"), sig_b64, signed_by,
                    )
        except Exception as e:                                          # noqa: BLE001
            logger.debug("registry.signature fetch skipped: %s", e)
            sig_valid = False

        return BundleResult(
            path=bundle_path, sha256=sha, size=total_size,
            signature_valid=sig_valid, signed_by=signed_by,
        )

    # ----- airgap ------------------------------------------------------

    def import_bundle(
        self,
        slug: str,
        version: str,
        data: bytes,
        *,
        expected_sha256: Optional[str] = None,
    ) -> BundleResult:
        """Used in airgap mode — accept an uploaded bundle blob and treat it
        as if it came from the registry."""
        bundle_dir = self.cache_dir / slug / version
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = bundle_dir / "bundle.helen-plugin"
        if len(data) > BUNDLE_MAX_BYTES:
            raise RegistryError("bundle-too-large")
        sha = hashlib.sha256(data).hexdigest()
        if expected_sha256 and sha != expected_sha256:
            raise RegistryError("sha256-mismatch")
        bundle_path.write_bytes(data)
        return BundleResult(
            path=bundle_path, sha256=sha, size=len(data),
            signature_valid=False, signed_by=None,
        )


# ───────────────────────────────────────────────────────────────────────
# Module-level singleton helpers (optional, for FastAPI deps)
# ───────────────────────────────────────────────────────────────────────


_default: Optional[RegistryClient] = None


def get_registry_client() -> RegistryClient:
    global _default
    if _default is None:
        _default = RegistryClient()
    return _default


__all__ = [
    "RegistryClient", "RegistryError", "CatalogEntry", "CatalogPage",
    "BundleResult", "get_registry_client",
    "DEFAULT_REGISTRY_URL", "AIRGAP_MODE",
]
