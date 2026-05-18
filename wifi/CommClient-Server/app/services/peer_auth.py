"""
Peer auth verification primitives.

Used by every acceptance mode (auto_accept / manual_approval /
pending / human_selection). Verification is the SAME in every mode;
the modes differ only in what happens AFTER verification succeeds.

Checks performed
----------------
1. Cluster ID match — incoming `cluster_id` must equal local config.
   Cluster isolation: a peer from cluster A can never join cluster B,
   regardless of mode or admin override.
2. HMAC signature — same shared `FEDERATION_SECRET` used elsewhere.
   Plus a peer-supplied signature over (server_id, cluster_id, nonce,
   timestamp, version, capabilities, public_key_fingerprint).
3. Timestamp / replay protection — request must be within
   ``FEDERATION_REPLAY_WINDOW_SECONDS`` of our clock.
4. Nonce uniqueness — short-lived dedup cache (5-minute window) so
   a recorded request can't be replayed even within the timestamp
   tolerance. Plus the deny-cache lookup (peers already rejected
   in the recent past don't need to go through verification again).
5. Version compatibility — incoming `version` must satisfy our
   minimum.
6. Capabilities — caller declares an explicit set; we accept any
   superset of the bare minimum (`fabric_v1`).

The output is a ``PeerVerifyResult`` carrying the final verdict plus
all the parsed fields so the approval service can persist them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Minimum version we accept. Bumped together with breaking schema
# changes. The peer's reported version must be >= this; we accept
# unknown future versions on the assumption that protocol evolution
# is forward-compatible.
MIN_PEER_VERSION = "1.0.0"

# Required capability set. A peer missing any of these can't usefully
# participate in the fabric.
REQUIRED_CAPABILITIES = frozenset({"fabric_v1"})

# Nonce dedup cache window. Longer than the replay window because
# the replay window is wall-clock; the nonce cache is per-process
# memory protected against replay even within the wall window.
NONCE_CACHE_TTL_SEC = 300

# Deny cache. A failed verification is remembered for this long so
# we can short-circuit without re-running expensive checks.
DEFAULT_DENY_CACHE_SEC = 300


@dataclass
class PeerVerifyResult:
    """Output of `verify_peer_candidate`. The boolean `ok` is the
    overall verdict; the other fields are populated even on failure
    so the approval service can persist context for the audit log."""

    ok: bool
    server_id: str = ""
    cluster_id: str = ""
    version: str = ""
    capabilities: set[str] = field(default_factory=set)
    public_key_fingerprint: str = ""
    endpoint: Optional[str] = None
    region: Optional[str] = None
    zone: Optional[str] = None
    discovery_method: Optional[str] = None

    # Failure context.
    failure_code: str = ""
    failure_detail: str = ""

    def reason(self) -> str:
        """Human-readable rejection reason for audit logs."""
        if self.ok:
            return ""
        return f"{self.failure_code}: {self.failure_detail}".strip(": ")


class _NonceCache:
    """In-memory nonce dedup. Process-local. Keyed by (server_id, nonce)
    so the same nonce arriving from two different source servers doesn't
    spuriously collide — that scenario is benign (different HMAC secrets
    in principle, different signatures) and used to break the discovery
    flow when the same announcement arrived via two channels (UDP + manual
    seed + federation gossip).

    Also caches the *signature* of the previously-seen payload so an
    idempotent retry of the EXACT same announcement is treated as a duplicate
    success rather than as a replay attack. If the signature differs but
    (server_id, nonce) match, that IS a replay and we refuse.

    Cross-process duplication isn't a problem because each process has its
    own clock window — a replay would have to land inside the same process's
    window AND pass HMAC, which is the same security federation_router
    seen_cache offers for chain routing."""

    def __init__(self, ttl_sec: float = NONCE_CACHE_TTL_SEC) -> None:
        self._ttl = ttl_sec
        # value is (recorded_at, signature) — signature lets us recognise
        # an idempotent retry of the same payload vs an actual nonce replay.
        self._seen: dict[tuple[str, str], tuple[float, str]] = {}
        self._lock = asyncio.Lock()
        self._max = 50_000  # cap to prevent memory blow-up

    async def remember(self, nonce: str, server_id: str = "", signature: str = "") -> str:
        """Record (server_id, nonce, signature) and return:
            "new"        — first time this (server_id, nonce) is seen.
            "idempotent" — same (server_id, nonce) AND same signature seen
                           recently. Caller should treat as success
                           (duplicate broadcast, multiple discovery channels).
            "replay"     — same (server_id, nonce) but DIFFERENT signature.
                           Real replay attack — caller MUST refuse.
        """
        async with self._lock:
            now = time.time()
            # Lazy eviction every Nth call to avoid an O(N) sweep
            # on every check.
            if len(self._seen) > self._max:
                cutoff = now - self._ttl
                self._seen = {k: v for k, v in self._seen.items() if v[0] > cutoff}
            key = (server_id, nonce)
            existing = self._seen.get(key)
            if existing is not None and (now - existing[0]) < self._ttl:
                prev_sig = existing[1]
                if signature and prev_sig and signature == prev_sig:
                    return "idempotent"
                return "replay"
            self._seen[key] = (now, signature)
            return "new"


class _DenyCache:
    """In-memory deny cache. Keyed by public_key_fingerprint so a
    peer that rotates keys leaves the cache and re-enters the
    approval flow naturally."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def is_denied(self, fingerprint: str) -> tuple[bool, str]:
        if not fingerprint:
            return False, ""
        async with self._lock:
            entry = self._entries.get(fingerprint)
            if entry is None:
                return False, ""
            expires_at, reason = entry
            if time.time() > expires_at:
                self._entries.pop(fingerprint, None)
                return False, ""
            return True, reason

    async def deny(self, fingerprint: str, reason: str, ttl_sec: int) -> None:
        if not fingerprint:
            return
        async with self._lock:
            self._entries[fingerprint] = (time.time() + ttl_sec, reason)

    async def clear(self, fingerprint: str) -> None:
        async with self._lock:
            self._entries.pop(fingerprint, None)


_nonce_cache = _NonceCache()
_deny_cache = _DenyCache()


# ── Public API ─────────────────────────────────────────────────────


def fingerprint_for_secret(secret: str) -> str:
    """Stable SHA-256 fingerprint of a peer's HMAC secret material.
    Used for deny-cache keys + audit. Never log the secret itself."""
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _signed_payload(
    *, server_id: str, cluster_id: str, nonce: str, timestamp: int,
    version: str, capabilities: set[str], public_key_fingerprint: str,
) -> bytes:
    parts = [
        server_id, cluster_id, nonce, str(timestamp), version,
        ",".join(sorted(capabilities)), public_key_fingerprint,
    ]
    return "|".join(parts).encode("utf-8")


def compute_signature(
    *, secret: str, server_id: str, cluster_id: str, nonce: str,
    timestamp: int, version: str, capabilities: set[str],
    public_key_fingerprint: str,
) -> str:
    """Construct the HMAC-SHA256 signature a peer must send. Symmetric
    helper — peers and verifier compute the same way."""
    body = _signed_payload(
        server_id=server_id, cluster_id=cluster_id, nonce=nonce,
        timestamp=timestamp, version=version, capabilities=capabilities,
        public_key_fingerprint=public_key_fingerprint,
    )
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return mac.hexdigest()


async def verify_peer_candidate(payload: dict) -> PeerVerifyResult:
    """Run every required check on an incoming peer announcement.
    Returns a PeerVerifyResult — caller persists & branches on
    ``result.ok``."""
    settings = get_settings()
    if not settings.COMMCLIENT_REQUIRE_PEER_AUTH:
        # Hard-disabled (lab dev only). Bypass with a warning.
        logger.warning("peer_auth_disabled_via_config")
        return _bypass_result(payload)

    # 1. Required fields.
    required = (
        "server_id", "cluster_id", "nonce", "timestamp",
        "version", "capabilities", "public_key_fingerprint", "signature",
    )
    missing = [k for k in required if not payload.get(k)]
    if missing:
        return _fail(payload, "missing_field", f"missing: {','.join(missing)}")

    server_id = str(payload["server_id"])
    cluster_id = str(payload["cluster_id"])
    nonce = str(payload["nonce"])
    try:
        timestamp = int(payload["timestamp"])
    except (TypeError, ValueError):
        return _fail(payload, "bad_timestamp", "timestamp not int")
    version = str(payload["version"])
    capabilities_raw = payload["capabilities"]
    if isinstance(capabilities_raw, str):
        capabilities = set(c.strip() for c in capabilities_raw.split(",") if c.strip())
    elif isinstance(capabilities_raw, (list, tuple, set)):
        capabilities = set(map(str, capabilities_raw))
    else:
        return _fail(payload, "bad_capabilities", "must be list or csv string")
    public_key_fingerprint = str(payload["public_key_fingerprint"])
    incoming_sig = str(payload["signature"])

    # 2. Deny-cache short-circuit.
    denied, reason = await _deny_cache.is_denied(public_key_fingerprint)
    if denied:
        return _fail(payload, "deny_cache", reason or "previously denied",
                     server_id=server_id, cluster_id=cluster_id,
                     fp=public_key_fingerprint)

    # 3. Cluster ID match.
    if settings.COMMCLIENT_REQUIRE_CLUSTER_ID_MATCH:
        local_cluster = settings.COMMCLIENT_CLUSTER_ID or ""
        if cluster_id != local_cluster:
            return _fail(payload, "cluster_mismatch",
                         f"peer={cluster_id} local={local_cluster}",
                         server_id=server_id, cluster_id=cluster_id,
                         fp=public_key_fingerprint)

    # 4. Timestamp / replay window.
    if settings.COMMCLIENT_REQUIRE_REPLAY_PROTECTION:
        now = int(time.time())
        skew = abs(now - timestamp)
        window = int(settings.FEDERATION_REPLAY_WINDOW_SECONDS or 60)
        if skew > window:
            return _fail(payload, "stale_timestamp",
                         f"skew={skew}s window={window}s",
                         server_id=server_id, cluster_id=cluster_id,
                         fp=public_key_fingerprint)

    # 5. Nonce uniqueness.
    # Note: we delay the nonce check until AFTER the HMAC verification below
    # would normally happen, because the dedup result depends on the signature
    # to distinguish idempotent retries from genuine replays. Here we record
    # the (server_id, nonce, incoming_sig) tuple. If the same (server_id,
    # nonce) was already seen with the SAME signature, treat as idempotent —
    # discovery is allowed to be multi-channel (UDP broadcast can arrive
    # alongside manual-seed probe and federation gossip), and rejecting the
    # second copy was breaking federation in v1.
    if settings.COMMCLIENT_REQUIRE_REPLAY_PROTECTION:
        result = await _nonce_cache.remember(nonce, server_id, incoming_sig)
        if result == "replay":
            return _fail(payload, "nonce_replay",
                         "nonce previously seen with different signature",
                         server_id=server_id, cluster_id=cluster_id,
                         fp=public_key_fingerprint)
        # "idempotent" and "new" both proceed to HMAC verification below;
        # idempotent retries still need to pass HMAC for safety in case the
        # cached signature is somehow spoofable.

    # 6. HMAC signature verification.
    if settings.COMMCLIENT_REQUIRE_SIGNATURE:
        secret = settings.FEDERATION_SECRET or ""
        if not secret or len(secret) < 16:
            # Misconfig on OUR side — refuse on principle.
            return _fail(payload, "local_secret_unconfigured",
                         "FEDERATION_SECRET missing or too short",
                         server_id=server_id, cluster_id=cluster_id,
                         fp=public_key_fingerprint)
        expected = compute_signature(
            secret=secret, server_id=server_id, cluster_id=cluster_id,
            nonce=nonce, timestamp=timestamp, version=version,
            capabilities=capabilities,
            public_key_fingerprint=public_key_fingerprint,
        )
        if not hmac.compare_digest(expected, incoming_sig):
            return _fail(payload, "bad_signature",
                         "HMAC mismatch",
                         server_id=server_id, cluster_id=cluster_id,
                         fp=public_key_fingerprint)

    # 7. Version compatibility.
    if not _version_at_least(version, MIN_PEER_VERSION):
        return _fail(payload, "version_too_old",
                     f"peer={version} min={MIN_PEER_VERSION}",
                     server_id=server_id, cluster_id=cluster_id,
                     fp=public_key_fingerprint)

    # 8. Capability check.
    missing_caps = REQUIRED_CAPABILITIES - capabilities
    if missing_caps:
        return _fail(payload, "missing_capabilities",
                     f"need: {','.join(sorted(missing_caps))}",
                     server_id=server_id, cluster_id=cluster_id,
                     fp=public_key_fingerprint)

    return PeerVerifyResult(
        ok=True,
        server_id=server_id,
        cluster_id=cluster_id,
        version=version,
        capabilities=capabilities,
        public_key_fingerprint=public_key_fingerprint,
        endpoint=payload.get("endpoint"),
        region=payload.get("region"),
        zone=payload.get("zone"),
        discovery_method=payload.get("discovery_method"),
    )


async def remember_denied(fingerprint: str, reason: str) -> None:
    """Push a fingerprint into the deny cache so subsequent
    discoveries short-circuit. Called by approval_service on
    reject/deny. TTL from settings."""
    if not fingerprint:
        return
    settings = get_settings()
    ttl = int(settings.COMMCLIENT_PEER_DENY_CACHE_SECONDS or DEFAULT_DENY_CACHE_SEC)
    await _deny_cache.deny(fingerprint, reason, ttl)


async def clear_denied(fingerprint: str) -> None:
    """Remove a fingerprint from the deny cache (admin override)."""
    await _deny_cache.clear(fingerprint)


# ── Internal helpers ───────────────────────────────────────────────


def _fail(
    payload: dict, code: str, detail: str,
    *, server_id: str = "", cluster_id: str = "", fp: str = "",
) -> PeerVerifyResult:
    return PeerVerifyResult(
        ok=False,
        server_id=server_id or str(payload.get("server_id", "")),
        cluster_id=cluster_id or str(payload.get("cluster_id", "")),
        public_key_fingerprint=fp or str(payload.get("public_key_fingerprint", "")),
        endpoint=payload.get("endpoint"),
        region=payload.get("region"),
        zone=payload.get("zone"),
        discovery_method=payload.get("discovery_method"),
        failure_code=code,
        failure_detail=detail,
    )


def _bypass_result(payload: dict) -> PeerVerifyResult:
    """When peer auth is hard-disabled in config — lab dev only.
    Construct an `ok=True` result from whatever the peer announced."""
    return PeerVerifyResult(
        ok=True,
        server_id=str(payload.get("server_id", "")),
        cluster_id=str(payload.get("cluster_id", "")),
        version=str(payload.get("version", "0.0.0")),
        capabilities=set(_normalize_caps(payload.get("capabilities"))),
        public_key_fingerprint=str(payload.get("public_key_fingerprint", "")),
        endpoint=payload.get("endpoint"),
        region=payload.get("region"),
        zone=payload.get("zone"),
        discovery_method=payload.get("discovery_method"),
    )


def _normalize_caps(raw) -> list[str]:
    if isinstance(raw, str):
        return [c.strip() for c in raw.split(",") if c.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(c) for c in raw]
    return []


def _version_at_least(actual: str, minimum: str) -> bool:
    """Compare semver-ish strings X.Y.Z. Treats unparseable versions
    as equivalent to 0.0.0 (rejects)."""
    def parse(s: str) -> tuple[int, int, int]:
        try:
            parts = s.strip().split(".")
            return (
                int(parts[0]) if len(parts) > 0 else 0,
                int(parts[1]) if len(parts) > 1 else 0,
                int(parts[2]) if len(parts) > 2 else 0,
            )
        except (ValueError, AttributeError):
            return (0, 0, 0)
    return parse(actual) >= parse(minimum)
