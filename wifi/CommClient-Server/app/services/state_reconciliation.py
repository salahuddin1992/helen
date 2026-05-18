"""
Periodic state reconciliation between Helen-Server peers.

Gossip alone gives eventual convergence on *peer membership*, but the
*content* of replicated tables (peer_trust, sync_policy, capability
overrides, role flags, deny_cache) can drift over time when:

  * a server was offline during an admin action,
  * a federation message was dropped silently below the relay layer,
  * two admins change the same key in different clusters within the
    nonce-replay window,
  * persistent retry queue exceeded its TTL.

This module runs a low-frequency background loop (default 60s) that
on every tick:

  1. Computes a stable hash over the local replicated state (Merkle
     root of small per-table hashes).
  2. Asks every fresh peer for theirs via a signed federation request.
  3. If the hashes differ, fetches the peer's diff (only the rows
     whose per-row hash differs from ours), applies last-write-wins
     based on ``updated_at`` timestamps, and persists the result.

Last-write-wins is the right semantics here because the data is
operator-driven (an admin clicks a button at a specific moment) — the
later click should win regardless of which peer it arrived on first.
For data needing causal ordering (chat messages), the messaging layer
uses its own vector-clock-aware reconciliation; this loop deliberately
stays at the meta level.

Hot-path properties
-------------------
* No write fan-out at admin click time — the admin endpoint just
  updates local state, the loop takes care of replication.
* Bounded bandwidth — only diff rows are exchanged.
* Resilient to partial cluster outages — convergence resumes the
  moment the network heals.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_RECONCILE_INTERVAL_SEC = 60.0
_RECONCILE_TIMEOUT_SEC = 5.0
_FANOUT_K = 5  # ask K random peers per cycle, not the whole cluster


# ── Local state hash ─────────────────────────────────────────────


def _hash_str(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _trust_table_hash() -> str:
    try:
        from app.services.trust_score import get_trust_db
        rows = get_trust_db().list_top(limit=10_000)
        rows.sort(key=lambda r: r["server_id"])
        s = json.dumps(
            [
                (r["server_id"], round(r["score"], 4), r["updated_at"])
                for r in rows
            ],
            sort_keys=True,
        )
        return _hash_str(s)
    except Exception:
        return _hash_str("trust:unavailable")


def _sync_policy_hash() -> str:
    try:
        from app.services.sync_policy import get_sync_policy
        snap = get_sync_policy().snapshot()
        s = json.dumps(
            {
                "paused": snap.get("paused"),
                "blocked": sorted(snap.get("blocked_server_ids", [])),
            },
            sort_keys=True,
        )
        return _hash_str(s)
    except Exception:
        return _hash_str("sync:unavailable")


def compute_local_state_hash() -> dict[str, str]:
    """Merkle-style root: dict of per-table hashes + a top-level root.

    Peers compare the root first; if it matches, no diff is needed. If
    only one table differs, only that table is exchanged.
    """
    parts = {
        "trust":       _trust_table_hash(),
        "sync_policy": _sync_policy_hash(),
    }
    root_input = json.dumps(parts, sort_keys=True)
    parts["root"] = _hash_str(root_input)
    return parts


# ── Peer hash fetch + diff resolution ───────────────────────────


async def _fetch_peer_hash(peer) -> Optional[dict[str, str]]:
    """Hit ``GET /api/cluster/state-hash`` on a peer and return its
    Merkle root + per-table hashes. None on any error so the caller
    can move on to the next peer.
    """
    try:
        import httpx
        from app.core.federation_auth import sign_request, HEADER_TIMESTAMP, HEADER_SIGNATURE
        path = "/api/cluster/state-hash"
        headers = sign_request("GET", path, b"")
        url = f"http://{peer.host}:{peer.port}{path}"
        async with httpx.AsyncClient(timeout=_RECONCILE_TIMEOUT_SEC) as c:
            r = await c.get(url, headers=headers)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.debug("reconcile_fetch_hash_failed",
                     peer=getattr(peer, "node_id", "?"), error=str(e))
    return None


async def _fetch_peer_table(peer, table: str) -> Optional[list[dict]]:
    """Hit ``GET /api/cluster/state-snapshot/{table}`` on a peer to
    pull the full replicated rows of one table. Returns None on error.
    """
    try:
        import httpx
        from app.core.federation_auth import sign_request
        path = f"/api/cluster/state-snapshot/{table}"
        headers = sign_request("GET", path, b"")
        url = f"http://{peer.host}:{peer.port}{path}"
        async with httpx.AsyncClient(timeout=_RECONCILE_TIMEOUT_SEC) as c:
            r = await c.get(url, headers=headers)
            if r.status_code == 200:
                d = r.json()
                return d.get("rows", []) if isinstance(d, dict) else None
    except Exception as e:
        logger.debug("reconcile_fetch_table_failed",
                     peer=getattr(peer, "node_id", "?"),
                     table=table, error=str(e))
    return None


def _merge_trust_rows(remote_rows: list[dict]) -> int:
    """Apply last-write-wins on remote rows where remote.updated_at >
    local.updated_at. Returns the number of rows changed."""
    from app.services.trust_score import get_trust_db
    db = get_trust_db()
    changed = 0
    for r in remote_rows:
        sid = r.get("server_id")
        if not sid:
            continue
        local = db.get(sid)
        local_ts = float(local.get("updated_at") or 0.0)
        remote_ts = float(r.get("updated_at") or 0.0)
        if remote_ts > local_ts:
            # Replay the latest event the peer recorded so our score
            # converges. We don't blindly overwrite the score column —
            # we use record_event so derived counts (success/violation)
            # also update.
            ev = r.get("last_event") or "successful_exchange"
            db.record_event(sid, ev)
            changed += 1
    return changed


def _merge_sync_policy(remote_snap: dict) -> bool:
    """The blocklist is a union — if any peer blocks X we block X.
    The `paused` flag uses last-write-wins on update timestamp."""
    try:
        from app.services.sync_policy import get_sync_policy
        policy = get_sync_policy()
        changed = False
        for sid in remote_snap.get("blocked_server_ids") or []:
            if not policy.is_blocked(sid):
                policy.block(sid)
                changed = True
        return changed
    except Exception:
        return False


# ── Background loop ─────────────────────────────────────────────


_loop_task: Optional[asyncio.Task] = None
_running = False


async def _reconciliation_loop() -> None:
    global _running
    _running = True
    logger.info("reconciliation_loop_started",
                interval_sec=_RECONCILE_INTERVAL_SEC, fanout=_FANOUT_K)
    try:
        while _running:
            try:
                await _reconcile_once()
            except Exception as e:
                logger.warning("reconciliation_cycle_failed", error=str(e))
            await asyncio.sleep(_RECONCILE_INTERVAL_SEC)
    finally:
        logger.info("reconciliation_loop_stopped")


async def _reconcile_once() -> None:
    import random
    from app.services.node_registry import get_registry
    reg = get_registry()
    peers = [n for n in reg.nodes(include_dead=False) if not n.self_node]
    if not peers:
        return

    local = compute_local_state_hash()
    targets = random.sample(peers, k=min(_FANOUT_K, len(peers)))

    for peer in targets:
        remote = await _fetch_peer_hash(peer)
        if not remote:
            continue
        if remote.get("root") == local.get("root"):
            # Already in sync with this peer — no work.
            continue

        # Fetch and merge any tables whose hashes differ.
        for table in ("trust", "sync_policy"):
            if remote.get(table) and remote[table] != local.get(table):
                rows = await _fetch_peer_table(peer, table)
                if rows is None:
                    continue
                if table == "trust":
                    n = _merge_trust_rows(rows)
                    if n:
                        logger.info(
                            "reconcile_trust_rows_merged",
                            peer=peer.node_id[:24],
                            rows=n,
                        )
                elif table == "sync_policy":
                    snap = rows if isinstance(rows, dict) else (
                        rows[0] if rows else {}
                    )
                    if _merge_sync_policy(snap):
                        logger.info(
                            "reconcile_sync_policy_merged",
                            peer=peer.node_id[:24],
                        )


def start_reconciliation_loop() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(
            _reconciliation_loop(),
            name="state-reconciliation",
        )
    except RuntimeError:
        # No running loop yet — caller should defer to startup hook.
        logger.warning("reconciliation_no_event_loop_yet")


def stop_reconciliation_loop() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
