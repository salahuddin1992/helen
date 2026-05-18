"""Service signing — HMAC over registration + heartbeat payloads.

Without authentication, anyone on the network could register
themselves as a high-capacity relay and steal traffic. This module
requires every registration / heartbeat to be signed with the
cluster HMAC secret (auto-derived from cluster_id, see
``core.federation_auth``). Signatures cover the canonical
service-id-binding fields so they can't be replayed across services.

Anti-spoofing properties:

  1. Cluster_id mismatch        → signature can't validate.
  2. Replay across service_ids  → signature includes service_id.
  3. Replay across timestamps   → signed_at must be recent
                                   (REPLAY_WINDOW_SEC, default 60).
  4. Tampering with capabilities → signature covers them too.

Caller workflow:

    payload = sign_record(record, cluster_id=...)
    POST /api/discovery/register {payload}

Receiver:

    if not verify_record(record):
        raise SignatureError
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Optional

from app.core.logging import get_logger
from app.service_discovery.discovery_exceptions import SignatureError
from app.service_discovery.service_record import ServiceRecord

logger = get_logger(__name__)


REPLAY_WINDOW_SEC = float(os.environ.get("HELEN_SD_REPLAY_WINDOW", "60"))


def _canonical_payload(record: ServiceRecord) -> bytes:
    """Stable byte representation for signing — order matters."""
    parts = [
        record.service_id,
        record.service_type.value,
        record.server_id,
        record.host,
        str(record.port),
        record.protocol,
        record.cluster_id,
        record.region,
        record.zone,
        str(int(record.signed_at)),
        str(int(record.ttl_sec)),
        str(int(record.max_capacity)),
    ]
    return "|".join(parts).encode("utf-8")


def _secret_for(cluster_id: str) -> bytes:
    """Resolve the HMAC key — same derivation as
    ``core.federation_auth._effective_secret`` so registrations and
    federation calls share trust. Falls back to a deterministic
    cluster-id-derived key when no explicit secret is set."""
    try:
        from app.core.federation_auth import _effective_secret
        return _effective_secret()
    except Exception:
        digest = hashlib.sha256(
            f"helen-lan-cluster:{cluster_id or 'default'}".encode()
        ).digest()
        return digest


def fingerprint(secret: bytes) -> str:
    """Public-safe identifier of the signing key — first 16 hex chars
    of sha256. Useful for ops to verify all peers are using the same
    cluster secret."""
    return hashlib.sha256(secret).hexdigest()[:16]


def sign_record(record: ServiceRecord) -> ServiceRecord:
    """Mutate ``record`` to attach a fresh signature + signed_at."""
    record.signed_at = time.time()
    secret = _secret_for(record.cluster_id)
    sig = hmac.new(
        secret, _canonical_payload(record), hashlib.sha256,
    ).hexdigest()
    record.signature = sig
    record.pubkey_fingerprint = fingerprint(secret)
    return record


def verify_record(record: ServiceRecord,
                  *, max_skew_sec: float = REPLAY_WINDOW_SEC) -> tuple[bool, str]:
    """Verify a record's signature + freshness. Returns (ok, reason)."""
    if not record.signature:
        return False, "missing_signature"
    if not record.signed_at:
        return False, "missing_signed_at"
    skew = abs(time.time() - record.signed_at)
    if skew > max_skew_sec:
        return False, f"stale_signature_skew={int(skew)}s"
    secret = _secret_for(record.cluster_id)
    expected = hmac.new(
        secret, _canonical_payload(record), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, record.signature):
        return False, "signature_mismatch"
    return True, "ok"


def require_valid(record: ServiceRecord) -> None:
    """Raise SignatureError if the record fails verification."""
    ok, reason = verify_record(record)
    if not ok:
        raise SignatureError(reason)


def status() -> dict:
    """Operator view of which key the local node is signing with."""
    cfg_cluster = "default"
    try:
        from app.core.config import get_settings
        cfg_cluster = get_settings().COMMCLIENT_CLUSTER_ID or "default"
    except Exception:
        pass
    secret = _secret_for(cfg_cluster)
    return {
        "cluster_id":          cfg_cluster,
        "fingerprint":         fingerprint(secret),
        "replay_window_sec":   REPLAY_WINDOW_SEC,
    }
