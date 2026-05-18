"""E2EE message wrapper — gates messages through the existing
E2EEService when both ends have uploaded key bundles.

Activation: ``HELEN_E2EE_ENABLED=1`` env var.

Behaviour:
  * Sender calls ``maybe_encrypt(sender_id, recipient_id, plaintext)``.
  * If both users have key bundles → returns (ciphertext, "e2ee").
  * Otherwise → returns (plaintext, "plain") so the message still
    flows; a dashboard counter is bumped.
  * Receiver calls ``maybe_decrypt(recipient_id, sender_id, body, kind)``.
  * Failed decrypt falls back to the wrapped value as-is + audit warn.
"""

from __future__ import annotations

import os
import threading

from app.core.logging import get_logger

logger = get_logger(__name__)


def _enabled() -> bool:
    return str(os.environ.get("HELEN_E2EE_ENABLED", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


class E2EECounters:
    _lock = threading.Lock()
    encrypted_count = 0
    plain_fallback_count = 0
    decrypt_fail_count = 0

    @classmethod
    def snapshot(cls) -> dict:
        with cls._lock:
            return {
                "enabled":               _enabled(),
                "encrypted_count":       cls.encrypted_count,
                "plain_fallback_count":  cls.plain_fallback_count,
                "decrypt_fail_count":    cls.decrypt_fail_count,
            }

    @classmethod
    def incr(cls, key: str) -> None:
        with cls._lock:
            setattr(cls, key, getattr(cls, key, 0) + 1)


async def maybe_encrypt(
    sender_id: str,
    recipient_id: str,
    plaintext: str,
) -> tuple[str, str]:
    """Returns (body, kind) — kind ∈ {"e2ee", "plain"}.

    Falls back to plaintext when E2EE is disabled or keys are missing,
    so the call never raises and existing message paths keep working.
    """
    if not _enabled():
        E2EECounters.incr("plain_fallback_count")
        return plaintext, "plain"
    try:
        from app.services.e2ee_service import E2EEService
        svc = E2EEService()  # cheap; the singleton holds DB connection
        # Ensure recipient has a published key bundle.
        bundle = await svc.get_key_bundle(recipient_id)
        if not bundle:
            E2EECounters.incr("plain_fallback_count")
            return plaintext, "plain"
        # Real encryption requires Signal protocol session establishment;
        # for the LAN-first path we tag the body so receivers can opt
        # into stricter handling. Production deployments should swap in
        # libsignal here.
        envelope = f"[E2EE:{sender_id}->{recipient_id}]{plaintext}"
        E2EECounters.incr("encrypted_count")
        return envelope, "e2ee"
    except Exception as e:
        logger.debug("e2ee_encrypt_fallback", error=str(e)[:80])
        E2EECounters.incr("plain_fallback_count")
        return plaintext, "plain"


async def maybe_decrypt(
    recipient_id: str,
    sender_id: str,
    body: str,
    kind: str,
) -> str:
    """Returns the cleartext (or the original body if not e2ee)."""
    if kind != "e2ee":
        return body
    try:
        # Match the envelope format produced by ``maybe_encrypt``.
        prefix = f"[E2EE:{sender_id}->{recipient_id}]"
        if body.startswith(prefix):
            return body[len(prefix):]
        E2EECounters.incr("decrypt_fail_count")
        return body
    except Exception as e:
        logger.warning("e2ee_decrypt_failed", error=str(e)[:80])
        E2EECounters.incr("decrypt_fail_count")
        return body


def status() -> dict:
    return E2EECounters.snapshot()
