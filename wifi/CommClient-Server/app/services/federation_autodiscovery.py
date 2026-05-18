"""
mDNS-based federation autodiscovery.

Different from ``mdns_discovery``
---------------------------------
``app.services.mdns_discovery`` advertises ``_helen-server._tcp.local.``
so any LAN client can find a Helen server. That's *general presence*.

This module advertises a separate service type
``_helen-fed._tcp.local.`` whose properties carry the **federation
endpoint** (``fed_port``, ``fed_scheme``, ``fed_token_fp``). Discovered
records get fed into a candidate-peers list that ``federation_service``
can consume to bootstrap inter-server trust without any manual
config-file editing.

Why a second service type
-------------------------
* Operators can run a Helen-Server that *participates in clients* but
  refuses to federate (different port, different secret). Splitting
  the announcement keeps that boundary clean.
* mDNS browsers can subscribe only to the channel they care about;
  federation autodiscovery doesn't churn whenever a desktop client
  comes online.
* Federation autodiscovery is opt-in (env-gated). General presence
  is on by default.

Wire shape
----------
Service type: ``_helen-fed._tcp.local.``
TXT properties:
    server_id     stable Helen-Server id (matches presence record)
    cluster_id    string, peers in same cluster auto-trust each other
    fed_port      int, federation listener port
    fed_scheme    "http" or "grpc"
    fed_token_fp  first 16 hex chars of HMAC-SHA256(federation_secret),
                  so peers can confirm they're using the same secret
                  without ever exchanging it
    version       Helen-Server version string

Discovered candidates accumulate in an in-memory ledger; the lifespan
or admin route can drain it via :func:`drain_candidates`. Trust
decisions stay with ``federation_service`` — this module only finds.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_SERVICE_TYPE = "_helen-fed._tcp.local."


def _zeroconf_available() -> bool:
    try:
        import zeroconf  # noqa: F401
        return True
    except ImportError:
        return False


# ── Candidate ledger ──────────────────────────────────────────────


@dataclass
class FederationCandidate:
    server_id: str
    host: str
    fed_port: int
    fed_scheme: str = "http"
    cluster_id: str = "default"
    token_fingerprint: Optional[str] = None
    version: Optional[str] = None
    discovered_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "server_id": self.server_id,
            "host": self.host,
            "fed_port": self.fed_port,
            "fed_scheme": self.fed_scheme,
            "cluster_id": self.cluster_id,
            "token_fingerprint": self.token_fingerprint,
            "version": self.version,
            "discovered_at": self.discovered_at,
            "last_seen_at": self.last_seen_at,
        }


class _CandidateLedger:
    """Thread-safe map keyed by server_id → FederationCandidate."""

    def __init__(self) -> None:
        self._data: dict[str, FederationCandidate] = {}
        self._lock = threading.Lock()

    def upsert(self, c: FederationCandidate) -> bool:
        """Returns True iff this is a NEW candidate (not seen before)."""
        with self._lock:
            existing = self._data.get(c.server_id)
            if existing:
                existing.last_seen_at = c.last_seen_at
                existing.host = c.host
                existing.fed_port = c.fed_port
                existing.fed_scheme = c.fed_scheme
                existing.cluster_id = c.cluster_id
                existing.token_fingerprint = c.token_fingerprint
                existing.version = c.version
                return False
            self._data[c.server_id] = c
            return True

    def all(self) -> list[FederationCandidate]:
        with self._lock:
            return list(self._data.values())

    def drain(self) -> list[FederationCandidate]:
        with self._lock:
            out = list(self._data.values())
            self._data.clear()
            return out

    def evict_older_than(self, cutoff_seconds: float) -> int:
        """Drop candidates not seen in the last ``cutoff_seconds``.

        Returns the number evicted."""
        now = time.time()
        with self._lock:
            stale = [
                sid for sid, c in self._data.items()
                if (now - c.last_seen_at) > cutoff_seconds
            ]
            for sid in stale:
                del self._data[sid]
            return len(stale)


# ── Token fingerprint ─────────────────────────────────────────────


def fingerprint_secret(secret: str) -> str:
    """Stable, leak-safe fingerprint of a federation secret. Two
    Helens that share the secret produce the same fingerprint; an
    eavesdropper can't recover the secret from the FP."""
    if not secret:
        return ""
    digest = hmac.new(b"helen-fed-fp/v1",
                       secret.encode("utf-8"),
                       hashlib.sha256).hexdigest()
    return digest[:16]


# ── mDNS listener ─────────────────────────────────────────────────


class _FederationListener:
    """zeroconf ServiceListener that pushes new federation peers into
    the candidate ledger."""

    def __init__(self, my_server_id: str, my_token_fp: str,
                  ledger: _CandidateLedger,
                  cluster_id_filter: Optional[str] = None,
                  require_token_match: bool = True) -> None:
        self.my_server_id = my_server_id
        self.my_token_fp = my_token_fp
        self.ledger = ledger
        self.cluster_id_filter = cluster_id_filter
        self.require_token_match = require_token_match

    def add_service(self, zc, type_, name):
        self._handle(zc, type_, name)

    def update_service(self, zc, type_, name):
        self._handle(zc, type_, name)

    def remove_service(self, zc, type_, name):
        return  # let the eviction sweep handle it

    def _handle(self, zc, type_, name) -> None:
        try:
            info = zc.get_service_info(type_, name, timeout=2000)
            if not info:
                return
            props: dict[str, str] = {}
            for k, v in (info.properties or {}).items():
                try:
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else v
                    props[key] = val
                except Exception:
                    continue

            sid = props.get("server_id") or ""
            if not sid or sid == self.my_server_id:
                return

            cluster_id = props.get("cluster_id") or "default"
            if (self.cluster_id_filter
                    and cluster_id != self.cluster_id_filter):
                logger.debug("fed_autodiscover_cluster_skip",
                             server_id=sid[:16], cluster=cluster_id)
                return

            their_fp = props.get("fed_token_fp") or ""
            if (self.require_token_match
                    and self.my_token_fp
                    and their_fp != self.my_token_fp):
                logger.debug("fed_autodiscover_token_fp_mismatch",
                             server_id=sid[:16],
                             their_fp=their_fp,
                             our_fp=self.my_token_fp)
                return

            host = (
                socket.inet_ntoa(info.addresses[0])
                if info.addresses
                else (info.server or "").rstrip(".")
            )
            try:
                fed_port = int(props.get("fed_port") or info.port or 0)
            except ValueError:
                fed_port = 0
            if fed_port <= 0:
                return

            cand = FederationCandidate(
                server_id=sid,
                host=host,
                fed_port=fed_port,
                fed_scheme=props.get("fed_scheme") or "http",
                cluster_id=cluster_id,
                token_fingerprint=their_fp or None,
                version=props.get("version"),
            )
            is_new = self.ledger.upsert(cand)
            if is_new:
                logger.info("fed_autodiscover_new_peer",
                            server_id=sid[:24], host=host,
                            fed_port=fed_port, scheme=cand.fed_scheme,
                            cluster=cluster_id)
        except Exception as e:
            logger.debug("fed_autodiscover_handle_failed",
                         name=name, error=str(e))


# ── Module-level state ────────────────────────────────────────────


_ledger = _CandidateLedger()
_zc = None
_browser = None
_service_info = None


def start_federation_autodiscovery(
    *,
    my_server_id: str,
    fed_port: int,
    fed_scheme: str = "http",
    cluster_id: str = "default",
    federation_secret: Optional[str] = None,
    advertise_host: Optional[str] = None,
    version: str = "?",
) -> bool:
    """Begin advertising + browsing federation services.

    Returns True iff zeroconf is available and the advertise/browse
    pair started successfully. Returns False (and logs a warning) if
    the optional ``zeroconf`` dependency is missing — caller can
    continue with manual federation config."""
    global _zc, _browser, _service_info

    if not _zeroconf_available():
        logger.warning("fed_autodiscover_no_zeroconf",
                       hint="pip install zeroconf to enable")
        return False

    try:
        from zeroconf import ServiceInfo, Zeroconf, ServiceBrowser, IPVersion
    except ImportError:
        return False

    my_token_fp = fingerprint_secret(federation_secret or "")

    _zc = Zeroconf(ip_version=IPVersion.V4Only)

    advertise_host = advertise_host or _local_lan_ip()
    fqdn = f"Helen-Fed-{my_server_id[:12]}.{_SERVICE_TYPE}"
    props = {
        "server_id": my_server_id,
        "cluster_id": cluster_id,
        "fed_port": str(fed_port),
        "fed_scheme": fed_scheme,
        "fed_token_fp": my_token_fp,
        "version": version,
    }
    addresses = [socket.inet_aton(advertise_host)] if advertise_host else []
    _service_info = ServiceInfo(
        _SERVICE_TYPE, fqdn,
        addresses=addresses,
        port=fed_port,
        properties=props,
        server=f"helen-fed-{my_server_id[:12]}.local.",
    )
    try:
        _zc.register_service(_service_info, allow_name_change=True)
    except Exception as e:
        logger.warning("fed_autodiscover_register_failed", error=str(e))

    _browser = ServiceBrowser(
        _zc, _SERVICE_TYPE,
        listener=_FederationListener(
            my_server_id=my_server_id,
            my_token_fp=my_token_fp,
            ledger=_ledger,
            cluster_id_filter=cluster_id,
            require_token_match=bool(federation_secret),
        ),
    )
    logger.info("fed_autodiscover_started",
                fed_port=fed_port, scheme=fed_scheme,
                cluster=cluster_id)
    return True


def stop_federation_autodiscovery() -> None:
    global _zc, _browser, _service_info
    try:
        if _service_info and _zc:
            _zc.unregister_service(_service_info)
    except Exception as e:
        logger.debug("fed_autodiscover_unregister_failed", error=str(e))
    try:
        if _browser:
            _browser.cancel()
    except Exception:
        pass
    try:
        if _zc:
            _zc.close()
    except Exception:
        pass
    _zc = None
    _browser = None
    _service_info = None


def list_candidates() -> list[FederationCandidate]:
    return _ledger.all()


def drain_candidates() -> list[FederationCandidate]:
    """Atomically pops every known candidate. Used by the federation
    bootstrap loop after it has consumed them."""
    return _ledger.drain()


def evict_stale(cutoff_seconds: float = 600.0) -> int:
    return _ledger.evict_older_than(cutoff_seconds)


# ── Local IP helper (no socket leak on failure) ───────────────────


def _local_lan_ip() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return None


# ── Env-driven start ──────────────────────────────────────────────


def configure_from_env(*, my_server_id: str,
                         federation_secret: Optional[str]) -> bool:
    """Start the autodiscovery channel iff
    ``HELEN_FEDERATION_AUTODISCOVER`` is truthy.

    Reads:
      HELEN_FEDERATION_AUTODISCOVER  on/off ("1", "true", "yes")
      HELEN_FEDERATION_PORT          int, default 3000
      HELEN_FEDERATION_BACKEND       "http" / "grpc"
      HELEN_FEDERATION_CLUSTER_ID    string
      HELEN_FEDERATION_ADVERTISE_HOST  optional override
      HELEN_FEDERATION_VERSION       optional version label
    """
    if os.environ.get(
        "HELEN_FEDERATION_AUTODISCOVER", "",
    ).lower() not in ("1", "true", "yes"):
        return False
    fed_port = int(os.environ.get("HELEN_FEDERATION_PORT", "3000"))
    fed_scheme = os.environ.get("HELEN_FEDERATION_BACKEND", "http")
    cluster_id = os.environ.get("HELEN_FEDERATION_CLUSTER_ID", "default")
    advertise_host = os.environ.get("HELEN_FEDERATION_ADVERTISE_HOST") or None
    version = os.environ.get("HELEN_FEDERATION_VERSION", "?")
    return start_federation_autodiscovery(
        my_server_id=my_server_id,
        fed_port=fed_port,
        fed_scheme=fed_scheme,
        cluster_id=cluster_id,
        federation_secret=federation_secret,
        advertise_host=advertise_host,
        version=version,
    )


__all__ = [
    "FederationCandidate",
    "fingerprint_secret",
    "start_federation_autodiscovery",
    "stop_federation_autodiscovery",
    "list_candidates",
    "drain_candidates",
    "evict_stale",
    "configure_from_env",
]
