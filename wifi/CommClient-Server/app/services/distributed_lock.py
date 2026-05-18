"""
Distributed lock — cluster-wide singleton coordination.

Some background tasks (audit-chain compactor, capacity recompute,
backup uploader) should run on exactly one Helen-Server in the
cluster at a time. Without coordination, every server tries the
work simultaneously, wasting cycles and producing conflicting
output.

This module gives those tasks a lease-based lock acquired through
the existing quorum-write primitive:

    async with distributed_lock("audit_compactor", ttl=300):
        await compact_audit_chain()

Mechanics
---------
1. Acquire = quorum_write(kind="lock", key="audit_compactor",
                          value={owner: my_id, expires_at: now+ttl}).
   * The current row is read first; if it's not expired and has a
     different owner, acquire fails immediately.
   * On success, ≥ ⌈K/2⌉+1 replicas now agree we hold it.
2. Renew = same quorum_write with a fresh ``expires_at``. Called
   automatically by the context manager every TTL/3.
3. Release = quorum_write with empty owner. Triggered on context exit.

Lease (not perpetual hold)
--------------------------
The TTL prevents permanent blockage: if the holder dies without
releasing, the lease expires and another peer can acquire. The
holder must renew at < TTL/3 to be safe under any drift / GC pause.

This is *not* a strict mutex — dynamo-style sloppy quorum can
briefly admit two holders during a partition. That's acceptable for
the workloads we use it on (idempotent maintenance tasks). For
strict mutex use a CP store (Postgres advisory lock, Zookeeper, etcd).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


DEFAULT_TTL_SEC = 300.0
RENEW_FACTOR = 3.0  # renew every TTL / RENEW_FACTOR

# Tracks live leases so a long-running body can poll is_lease_alive(name)
# and break out cleanly when the renewer can no longer prove ownership.
# This is a *local* view — the actual quorum is the source of truth.
_active_leases: dict[str, _LeaseToken] = {}


@dataclass
class _LeaseToken:
    name:        str
    owner:       str
    acquired_at: float
    expires_at:  float


# ── Internal ────────────────────────────────────────────────────


def _self_id() -> str:
    try:
        from app.services.discovery_service import get_server_id
        return get_server_id() or "anon"
    except Exception:
        return "anon"


async def _try_acquire(
    name: str,
    owner: str,
    ttl: float,
) -> Optional[_LeaseToken]:
    """Compare-and-swap acquire: read current state, decide if we can
    take it, then quorum-write with `expected_version` matching what
    we read. The replication layer rejects the write if another peer
    bumped the version between our read and write — closing the race
    window that previously admitted two simultaneous holders.

    If the underlying replication layer doesn't expose
    ``expected_version`` (older deployments), we fall back to the
    LWW-only path which still gives strong-enough guarantees for
    idempotent maintenance jobs."""
    try:
        from app.services.replication_manager import get as rep_get
        from app.services.quorum_decision import quorum_write
    except ImportError:
        return None

    now = time.time()
    existing = rep_get("lock", name)
    expected_version = 0
    if existing:
        cur = existing.get("value") or {}
        cur_owner = cur.get("owner") or ""
        cur_expires = float(cur.get("expires_at") or 0)
        expected_version = int(existing.get("version") or 0)
        if cur_owner and cur_owner != owner and cur_expires > now:
            # Held by someone else, not yet expired.
            return None
        # Stale or our own — proceed to claim with CAS on the version
        # we just observed. Two peers reading at the same moment will
        # both see expected_version=N; only one's quorum_write at
        # version=N+1 will succeed; the other's will be rejected.

    expires_at = now + ttl
    write_kwargs = {
        "kind": "lock",
        "key":  name,
        "value": {
            "owner":       owner,
            "acquired_at": now,
            "expires_at":  expires_at,
        },
    }
    # Pass expected_version when the underlying API supports it.
    # `quorum_write` may ignore unknown kwargs (TypeError) — we catch
    # and retry without the CAS hint to stay backwards-compatible.
    try:
        result = await quorum_write(**write_kwargs, expected_version=expected_version)
    except TypeError:
        result = await quorum_write(**write_kwargs)
    if not result.accepted:
        return None
    return _LeaseToken(
        name=name, owner=owner,
        acquired_at=now, expires_at=expires_at,
    )


async def _renew(token: _LeaseToken, ttl: float) -> bool:
    try:
        from app.services.quorum_decision import quorum_write
    except ImportError:
        return False
    now = time.time()
    new_expires = now + ttl
    result = await quorum_write(
        kind="lock", key=token.name,
        value={
            "owner":       token.owner,
            "acquired_at": token.acquired_at,
            "expires_at":  new_expires,
        },
    )
    if result.accepted:
        token.expires_at = new_expires
        return True
    return False


async def _release(token: _LeaseToken) -> None:
    try:
        from app.services.quorum_decision import quorum_write
    except ImportError:
        return
    await quorum_write(
        kind="lock", key=token.name,
        value={"owner": "", "acquired_at": 0, "expires_at": 0},
    )


# ── Public context manager ──────────────────────────────────────


@contextlib.asynccontextmanager
async def distributed_lock(
    name: str,
    *,
    ttl: float = DEFAULT_TTL_SEC,
    acquire_timeout: float = 5.0,
    poll_interval: float = 1.0,
) -> AsyncIterator[bool]:
    """Acquire ``name`` cluster-wide for at most ``ttl`` seconds and
    auto-renew while the body runs. Yields True on acquire, False if
    we couldn't (the body still runs — caller decides what to do).

        async with distributed_lock("audit_compactor", ttl=300) as held:
            if not held:
                return  # someone else owns it
            await compact_audit_chain()
    """
    owner = _self_id()
    token: Optional[_LeaseToken] = None
    deadline = time.time() + acquire_timeout
    while time.time() < deadline:
        token = await _try_acquire(name, owner, ttl)
        if token:
            break
        await asyncio.sleep(poll_interval)

    if token is None:
        logger.info("distributed_lock_not_acquired", name=name)
        try:
            yield False
        finally:
            return

    logger.info(
        "distributed_lock_acquired",
        name=name, owner=owner[:24],
        ttl=ttl, expires_at=round(token.expires_at, 1),
    )
    _active_leases[name] = token

    # Renewal task.
    renewed = True

    async def _renewer():
        nonlocal renewed
        try:
            while renewed:
                await asyncio.sleep(ttl / RENEW_FACTOR)
                if not renewed:
                    return
                ok = await _renew(token, ttl)
                if not ok:
                    renewed = False
                    _active_leases.pop(name, None)
                    logger.warning(
                        "distributed_lock_renew_failed",
                        name=name,
                    )
                    return
        except asyncio.CancelledError:
            pass

    renew_task = asyncio.create_task(_renewer(), name=f"lock-renew-{name}")

    try:
        yield True
    finally:
        renewed = False
        _active_leases.pop(name, None)
        renew_task.cancel()
        try:
            await renew_task
        except asyncio.CancelledError:
            pass
        await _release(token)
        logger.info("distributed_lock_released", name=name)


def is_lease_alive(name: str) -> bool:
    """True if this process currently believes it holds ``name`` and
    the lease hasn't yet expired locally. Long-running critical
    sections should poll this to bail out cleanly when the renewer
    has lost the lease (e.g. due to a partition).

        async with distributed_lock("compactor", ttl=300) as held:
            if not held: return
            for chunk in big_work():
                if not is_lease_alive("compactor"):
                    break  # lost the lease, stop before another holder collides
                process(chunk)
    """
    tok = _active_leases.get(name)
    if tok is None:
        return False
    return time.time() < tok.expires_at


# ── Diagnostics ─────────────────────────────────────────────────


def lock_status(name: str) -> dict:
    """Local view of a named lock — what owner is recorded, when
    it expires."""
    try:
        from app.services.replication_manager import get as rep_get
    except ImportError:
        return {"name": name, "exists": False}
    rec = rep_get("lock", name)
    if not rec:
        return {"name": name, "exists": False}
    val = rec.get("value") or {}
    expires = float(val.get("expires_at") or 0)
    return {
        "name":        name,
        "exists":      True,
        "owner":       val.get("owner"),
        "acquired_at": val.get("acquired_at"),
        "expires_at":  expires,
        "ttl_remaining": max(0.0, round(expires - time.time(), 1)),
    }
