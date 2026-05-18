"""
SIEM chain pub-sub wrapper.

The legacy ``app.services.audit_chain.AuditChain`` is append-only and has
no notification surface. The SIEM dashboard, alert engine, and WebSocket
fan-out all need to react to new audit entries in real time.

Rather than rewrite the chain, this module:

1. Re-exports the legacy chain singleton so callers can ``from
   app.services.audit.chain import get_audit_chain`` without learning
   the old import path.

2. Adds ``subscribe(callback)`` / ``unsubscribe(callback)`` and a
   ``publish(entry)`` hook. ``audit_log()`` in ``app.core.audit`` already
   calls ``chain.append(...)`` — we monkey-patch ``AuditChain.append``
   exactly once to forward the resulting entry to subscribers. Existing
   chain semantics (atomic append, hash-linking, verify) are preserved
   byte-for-byte; the patch wraps, never replaces.

3. Provides a higher-level ``head_info()`` that returns the dict the
   ``/api/admin/audit/head`` endpoint serves, including verify status.

Thread + asyncio safety
-----------------------
Subscribers can be sync or async callables. We invoke each one with
``asyncio.create_task`` when an event loop is running, else we run the
sync ones inline and skip async ones (chain append may run from a sync
context — e.g. a background thread — and we never want a subscriber
fault to break the original append transaction).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Awaitable, Callable, Optional, Union

from app.core.logging import get_logger
from app.services.audit_chain import (  # re-export
    AuditChain,
    AuditEntry,
    GENESIS_HASH,
    configure_audit_chain,
    get_audit_chain,
)

logger = get_logger(__name__)

Subscriber = Callable[[AuditEntry], Union[None, Awaitable[None]]]

_subscribers: list[Subscriber] = []
_subscribers_lock = threading.Lock()
_patched = False
_patch_lock = threading.Lock()


def subscribe(callback: Subscriber) -> None:
    """Register a callback for every newly appended audit entry.

    The callback receives an ``AuditEntry`` instance. May be sync or async.
    Idempotent — duplicates are silently dropped.
    """
    with _subscribers_lock:
        if callback not in _subscribers:
            _subscribers.append(callback)
    _ensure_patched()


def unsubscribe(callback: Subscriber) -> None:
    with _subscribers_lock:
        try:
            _subscribers.remove(callback)
        except ValueError:
            pass


def _snapshot_subscribers() -> list[Subscriber]:
    with _subscribers_lock:
        return list(_subscribers)


def publish(entry: AuditEntry) -> None:
    """Fan an entry out to all current subscribers.

    Never raises. Subscriber faults are logged and swallowed so the
    append path stays safe.
    """
    subs = _snapshot_subscribers()
    if not subs:
        return

    # If we're inside an asyncio loop, schedule async callbacks; sync
    # ones run inline (cheap — emit-only). If there's no loop (we were
    # called from a background thread), we run sync callbacks inline
    # and skip the async ones (they're rare for the SIEM use case).
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    for cb in subs:
        try:
            result = cb(entry)
            if asyncio.iscoroutine(result):
                if loop is not None:
                    loop.create_task(_swallow_async(cb, result))
                else:
                    # Best effort: spin a temporary loop on a dedicated thread
                    threading.Thread(
                        target=_run_coro_threadsafe,
                        args=(cb, result),
                        daemon=True,
                    ).start()
        except Exception as exc:
            logger.warning("audit_subscriber_failed",
                           callback=getattr(cb, "__name__", repr(cb)),
                           error=str(exc))


async def _swallow_async(cb: Subscriber, coro: Awaitable[None]) -> None:
    try:
        await coro
    except Exception as exc:
        logger.warning("audit_subscriber_async_failed",
                       callback=getattr(cb, "__name__", repr(cb)),
                       error=str(exc))


def _run_coro_threadsafe(cb: Subscriber, coro: Awaitable[None]) -> None:
    try:
        asyncio.run(_swallow_async(cb, coro))
    except Exception as exc:
        logger.warning("audit_subscriber_thread_failed",
                       callback=getattr(cb, "__name__", repr(cb)),
                       error=str(exc))


_ORIGINAL_APPEND = None  # captured once on first patch


def _ensure_patched() -> None:
    """Monkey-patch ``AuditChain.append`` exactly once so it calls
    ``publish(entry)`` after a successful append. Idempotent — safe to
    call repeatedly (and in tests after reseting ``_patched``)."""
    global _patched, _ORIGINAL_APPEND
    if _patched:
        return
    with _patch_lock:
        if _patched:
            return
        if _ORIGINAL_APPEND is None:
            _ORIGINAL_APPEND = AuditChain.append  # type: ignore[assignment]

        original_append = _ORIGINAL_APPEND

        def patched_append(self: AuditChain, actor: str, action: str,
                           *, target: Optional[str] = None,
                           payload: Optional[dict] = None) -> AuditEntry:
            entry = original_append(self, actor, action,
                                    target=target, payload=payload)
            try:
                publish(entry)
            except Exception as exc:
                logger.debug("audit_publish_failed", error=str(exc))
            return entry

        AuditChain.append = patched_append  # type: ignore[assignment]
        _patched = True
        logger.info("audit_chain_pubsub_hook_installed")


# Eagerly install the patch on import so subscribers attached *before*
# any append call see all events.
_ensure_patched()


# ── Higher-level helpers used by the REST API ───────────────────────────

def head_info(verify: bool = False) -> dict[str, Any]:
    """Return the head record + verify status for the dashboard.

    ``verify=True`` walks the chain (cheap for small chains; the REST
    handler should pass ``False`` and let the operator click "Verify"
    explicitly when the chain is large).
    """
    chain_inst = get_audit_chain()
    if chain_inst is None:
        return {
            "index": 0,
            "prev_hash": GENESIS_HASH,
            "entry_hash": None,
            "timestamp": None,
            "verify_status": "unknown",
            "configured": False,
        }
    head = chain_inst.head()
    status = "unknown"
    if verify:
        try:
            ok, broken_at, _msg = chain_inst.verify()
            status = "ok" if ok else "tampered"
        except Exception:
            status = "unknown"
    return {
        "index": head.seq if head else 0,
        "prev_hash": head.prev_hash if head else GENESIS_HASH,
        "entry_hash": head.chain_hash if head else None,
        "timestamp": head.timestamp if head else None,
        "verify_status": status,
        "configured": True,
        "now": time.time(),
    }


__all__ = [
    "AuditChain",
    "AuditEntry",
    "GENESIS_HASH",
    "configure_audit_chain",
    "get_audit_chain",
    "subscribe",
    "unsubscribe",
    "publish",
    "head_info",
]
