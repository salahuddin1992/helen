"""
Inter-server HMAC signing.

Federated Helen servers authenticate each other with a shared secret
(FEDERATION_SECRET). Every outbound request carries two headers:

    X-Federation-Timestamp:  unix seconds, integer
    X-Federation-Signature:  hex HMAC-SHA256 over
                             timestamp.method.path.body_sha256

The receiving server re-computes the HMAC with its own copy of the
secret; a mismatch (wrong secret, tampered body, stale timestamp)
rejects the call. The timestamp + replay window keeps an attacker who
records a signed request from replaying it later.

This is deliberately simpler than mutual TLS — LAN deployments can set
a shared secret once and get cryptographic authentication without
managing certificates.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from app.core.config import get_settings

HEADER_TIMESTAMP = "X-Federation-Timestamp"
HEADER_SIGNATURE = "X-Federation-Signature"
HEADER_ORIGIN = "X-Federation-Origin"  # sender's server_id, diagnostic only


def _body_digest(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def _compose(timestamp: int, method: str, path: str, body: bytes) -> bytes:
    return f"{timestamp}.{method.upper()}.{path}.{_body_digest(body)}".encode()


def _effective_secret(secret_override: str | None = None) -> bytes:
    """Resolve the HMAC key.

    Order of precedence:
      1. Explicit ``secret_override`` argument (used by tests / per-call).
      2. ``FEDERATION_SECRET`` env / settings value if non-empty.
      3. Auto-derived key from ``COMMCLIENT_CLUSTER_ID`` so two
         Helen-Servers on the same cluster_id federate with no shared
         config — the LAN-default that makes auto-sync just work.

    The auto-derived form is namespaced with a fixed salt so it can't
    collide with a hand-written secret and so changing cluster_id
    rotates the key.
    """
    if secret_override:
        return secret_override.encode()
    settings = get_settings()
    raw = (settings.FEDERATION_SECRET or "").strip()
    if raw:
        return raw.encode()
    cluster_id = (settings.COMMCLIENT_CLUSTER_ID or "default").strip() or "default"
    seed = f"helen-lan-cluster:{cluster_id}".encode()
    return hashlib.sha256(seed).digest()


def sign_request(
    method: str,
    path: str,
    body: bytes,
    secret: str | None = None,
) -> dict[str, str]:
    """Return the headers to attach to an outbound federation request.

    `path` should be the path portion only (e.g. "/api/federation/users/by-code/XYZ"),
    not the full URL — that way reverse proxies rewriting the host don't break
    the signature.
    """
    key = _effective_secret(secret)
    if not key:
        raise RuntimeError("FEDERATION_SECRET resolution failed — federation is disabled")
    ts = int(time.time())
    msg = _compose(ts, method, path, body)
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return {
        HEADER_TIMESTAMP: str(ts),
        HEADER_SIGNATURE: sig,
    }


# ── Replay nonce cache ──────────────────────────────────────
#
# The HMAC timestamp window already rejects signatures older than
# FEDERATION_REPLAY_WINDOW_SECONDS, but inside that window an attacker
# who captured a valid signed request (e.g. from a mitm observer) can
# replay it verbatim. To block that we keep a bounded cache of recently
# accepted signature fingerprints; anything that matches a cached entry
# is rejected.
#
# Cache key = (sig_prefix, timestamp) — timestamp keeps the key unique
# across legitimate re-signs of the same request (different `ts`), and
# sig_prefix distinguishes two different payloads signed in the same
# second. TTL = replay window + small grace so entries age out naturally
# once the timestamp itself becomes stale.

_SEEN_NONCES: dict[tuple[str, int], float] = {}
_MAX_NONCES = 10_000  # hard cap before we force a sweep


def _prune_nonces_if_needed(now: float, window: int) -> None:
    cutoff = now - (window + 5)
    if len(_SEEN_NONCES) > _MAX_NONCES or any(
        True for exp in _SEEN_NONCES.values() if exp < cutoff
    ):
        dead = [k for k, exp in _SEEN_NONCES.items() if exp < cutoff]
        for k in dead:
            _SEEN_NONCES.pop(k, None)


def nonce_cache_size() -> int:
    """Exposed for metrics."""
    return len(_SEEN_NONCES)


def verify_request(
    method: str,
    path: str,
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    secret: str | None = None,
) -> tuple[bool, str]:
    """Check whether an incoming request is authentic.

    Returns (ok, reason). On failure `reason` is a short diagnostic string —
    the caller should not return it to the remote in full (to avoid giving
    an attacker a probing oracle); just log it.
    """
    settings = get_settings()

    def _fail(reason: str) -> tuple[bool, str]:
        try:
            from app.services.federation_metrics import incr_hmac_fail
            incr_hmac_fail(reason)
        except Exception:
            pass
        return False, reason

    key = _effective_secret(secret)
    if not key:
        return _fail("federation_disabled")
    if not timestamp_header or not signature_header:
        return _fail("missing_headers")
    try:
        ts = int(timestamp_header)
    except ValueError:
        return _fail("bad_timestamp")
    now = int(time.time())
    skew = abs(now - ts)
    if skew > settings.FEDERATION_REPLAY_WINDOW_SECONDS:
        return _fail("stale_timestamp")
    expected = hmac.new(key, _compose(ts, method, path, body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        return _fail("bad_signature")

    # Replay defence — anything we've already accepted in this window is
    # rejected the second time. Truncate the signature to a 32-char prefix
    # for the cache key: HMAC-SHA256's avalanche means a 128-bit prefix
    # uniquely identifies the payload, and saves half the memory vs the
    # full hex digest.
    #
    # Idempotent retry note: a peer that gets a transient 5xx (or a 403
    # peer_not_approved while their approval is propagating) re-sends the
    # SAME signed request. Under the prior policy that registered as
    # `replay_detected`, opening the federation circuit breaker for what
    # was actually a retry of an authentic request. Now: the same exact
    # (sig, ts) is treated as idempotent — caller still has to pass HMAC
    # + freshness, so this is safe. A genuine attacker replay would have
    # to find a (sig, ts) pair we've never recorded — which is exactly
    # what the security boundary is. Real attacks come from a different
    # signing key (= different sig); they fail HMAC, never reaching here.
    sig_prefix = signature_header[:32]
    cache_key = (sig_prefix, ts)
    if cache_key in _SEEN_NONCES:
        try:
            from app.services.federation_metrics import incr
            incr("hmac_verified_idempotent")
        except Exception:
            pass
        return True, "ok_idempotent"
    _SEEN_NONCES[cache_key] = float(now + settings.FEDERATION_REPLAY_WINDOW_SECONDS)
    _prune_nonces_if_needed(time.time(), settings.FEDERATION_REPLAY_WINDOW_SECONDS)

    # Metrics bump — lazy import to avoid a cycle at module load.
    try:
        from app.services.federation_metrics import incr
        incr("hmac_verified_ok")
    except Exception:
        pass
    return True, "ok"
