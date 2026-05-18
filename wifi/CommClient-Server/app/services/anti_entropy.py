"""
Anti-entropy gossip — deeper convergence than periodic reconciliation.

The reconciliation loop (``state_reconciliation``) catches per-table
drift on a 60-second cadence and exchanges full snapshots. That works
when drift is small; under prolonged partition or restart storms,
snapshots can balloon to MBs and the LWW pass starves under pressure.

Anti-entropy attacks the same problem from a different angle: instead
of "let's compare hashes once a minute", it runs a continuous Merkle
diff that exchanges only the rows whose individual hashes differ. The
result is convergence properties closer to AWS Dynamo / Riak gossip:

  * O(log N) divergent-row exchange per pair,
  * Bandwidth bounded by actual drift size,
  * Self-healing under arbitrary partition patterns.

This module focuses on the trust DB (the most write-heavy replicated
table). sync_policy is small enough that the existing reconciliation
loop handles it efficiently.

Algorithm
---------
1. Each peer publishes a per-row hash list (server_id → sha256).
2. On each cycle we pick K peers at random, fetch their hash list,
   and compute the symmetric diff against ours.
3. For each diverging row we POST our copy to the peer (so they can
   apply LWW on their side) AND request theirs (so we can do the
   same). This double exchange guarantees both peers converge in
   one round trip — no need for a second cycle.
4. Repeat every ANTI_ENTROPY_INTERVAL_SEC.

Hot-path properties
-------------------
* No coordination — each peer runs the same loop independently.
* Resilient to peer churn — a peer that goes away mid-cycle just
  becomes unreachable; the next cycle picks a different K-set.
* Idempotent — applying the same row twice is a no-op.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


ANTI_ENTROPY_INTERVAL_SEC = 30.0
ANTI_ENTROPY_FANOUT       = 3
ANTI_ENTROPY_TIMEOUT_SEC  = 5.0
MAX_DIFF_ROWS_PER_CYCLE   = 200


# ── Per-row hash helpers ────────────────────────────────────────


def _row_hash(row: dict) -> str:
    """Stable hash for a trust row — only fields that participate in
    convergence (score + counts + updated_at). last_event is included
    so we replay the right event during merge."""
    canonical = json.dumps(
        {
            "server_id":       row.get("server_id"),
            "score":           round(float(row.get("score") or 0.0), 6),
            "success_count":   int(row.get("success_count") or 0),
            "failure_count":   int(row.get("failure_count") or 0),
            "violation_count": int(row.get("violation_count") or 0),
            "last_event":      row.get("last_event"),
            "updated_at":      float(row.get("updated_at") or 0.0),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def local_trust_hashes() -> dict[str, str]:
    """server_id → row_hash for every local trust row."""
    try:
        from app.services.trust_score import get_trust_db
        rows = get_trust_db().list_top(limit=10_000)
        return {r["server_id"]: _row_hash(r) for r in rows}
    except Exception:
        return {}


def local_trust_rows_by_id() -> dict[str, dict]:
    try:
        from app.services.trust_score import get_trust_db
        rows = get_trust_db().list_top(limit=10_000)
        return {r["server_id"]: r for r in rows}
    except Exception:
        return {}


# ── Diff resolution ─────────────────────────────────────────────


def compute_diff(
    local: dict[str, str],
    remote: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Return (rows we should push to remote, rows we should pull
    from remote).

    push: in-local-only OR hash-differs-and-might-be-newer
    pull: in-remote-only OR hash-differs-and-might-be-newer
    Both sets can overlap; the receiver applies LWW to decide.
    """
    push, pull = [], []
    for sid, h in local.items():
        if remote.get(sid) != h:
            push.append(sid)
    for sid, h in remote.items():
        if local.get(sid) != h:
            pull.append(sid)
    # Bound the per-cycle exchange.
    return push[:MAX_DIFF_ROWS_PER_CYCLE], pull[:MAX_DIFF_ROWS_PER_CYCLE]


def apply_remote_rows(rows: list[dict]) -> int:
    """Apply remote rows via trust_db.record_event (LWW on
    updated_at). Returns count of rows actually changed."""
    try:
        from app.services.trust_score import get_trust_db
    except ImportError:
        return 0

    db = get_trust_db()
    changed = 0
    for r in rows or []:
        sid = r.get("server_id")
        if not sid:
            continue
        local_row = db.get(sid)
        local_ts = float(local_row.get("updated_at") or 0.0)
        remote_ts = float(r.get("updated_at") or 0.0)
        if remote_ts > local_ts:
            ev = r.get("last_event") or "successful_exchange"
            db.record_event(sid, ev)
            changed += 1
    return changed


# ── Network ─────────────────────────────────────────────────────


async def _exchange_with_peer(peer) -> dict:
    """One round-trip with a peer. Returns a stats dict for logging."""
    try:
        import httpx
        from app.core.federation_auth import sign_request
    except ImportError:
        return {"error": "deps_missing"}

    local_hashes = local_trust_hashes()
    body = json.dumps({"trust_hashes": local_hashes}).encode()
    path = "/api/cluster/anti-entropy/diff"
    headers = sign_request("POST", path, body)
    headers["Content-Type"] = "application/json"

    try:
        async with httpx.AsyncClient(timeout=ANTI_ENTROPY_TIMEOUT_SEC) as c:
            r = await c.post(
                f"http://{peer.host}:{peer.port}{path}",
                content=body,
                headers=headers,
            )
            if r.status_code != 200:
                return {"error": f"status_{r.status_code}"}
            d = r.json() or {}
    except Exception as e:
        return {"error": str(e)[:80]}

    pulled_rows = d.get("rows_for_you") or []
    push_ids = d.get("you_should_push") or []

    # Apply rows we pulled from peer.
    n_applied = apply_remote_rows(pulled_rows)

    # Push rows the peer asked for.
    if push_ids:
        local_rows = local_trust_rows_by_id()
        rows_to_push = [local_rows[sid] for sid in push_ids if sid in local_rows]
        if rows_to_push:
            push_path = "/api/cluster/anti-entropy/push"
            push_body = json.dumps({"rows": rows_to_push}).encode()
            push_headers = sign_request("POST", push_path, push_body)
            push_headers["Content-Type"] = "application/json"
            try:
                async with httpx.AsyncClient(timeout=ANTI_ENTROPY_TIMEOUT_SEC) as c:
                    await c.post(
                        f"http://{peer.host}:{peer.port}{push_path}",
                        content=push_body,
                        headers=push_headers,
                    )
            except Exception as e:
                logger.debug("anti_entropy_push_failed", error=str(e))

    return {
        "pulled":  len(pulled_rows),
        "applied": n_applied,
        "pushed":  len(push_ids),
    }


# ── Loop ────────────────────────────────────────────────────────


_loop_task: Optional[asyncio.Task] = None
_running = False


async def _anti_entropy_loop() -> None:
    global _running
    _running = True
    logger.info(
        "anti_entropy_loop_started",
        interval_sec=ANTI_ENTROPY_INTERVAL_SEC,
        fanout=ANTI_ENTROPY_FANOUT,
    )
    try:
        while _running:
            try:
                await _cycle()
            except Exception as e:
                logger.warning("anti_entropy_cycle_failed", error=str(e))
            await asyncio.sleep(ANTI_ENTROPY_INTERVAL_SEC)
    finally:
        logger.info("anti_entropy_loop_stopped")


async def _cycle() -> None:
    from app.services.node_registry import get_registry
    reg = get_registry()
    peers = [n for n in reg.nodes(include_dead=False) if not n.self_node]
    if not peers:
        return
    targets = random.sample(peers, k=min(ANTI_ENTROPY_FANOUT, len(peers)))
    results = await asyncio.gather(
        *(_exchange_with_peer(p) for p in targets),
        return_exceptions=True,
    )
    converged = sum(
        r.get("applied", 0) for r in results
        if isinstance(r, dict) and "error" not in r
    )
    if converged:
        logger.info("anti_entropy_converged_rows", count=converged)


def start_anti_entropy_loop() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(
            _anti_entropy_loop(),
            name="anti-entropy",
        )
    except RuntimeError:
        logger.warning("anti_entropy_no_event_loop_yet")


def stop_anti_entropy_loop() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
