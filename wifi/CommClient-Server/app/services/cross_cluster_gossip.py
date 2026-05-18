"""Cross-cluster gossip — exchange peer summaries between clusters.

Same-cluster gossip uses ``services.anti_entropy``. For peers in
*foreign* clusters, that path doesn't apply (different HMAC key per
cluster). This module runs a periodic out-of-band exchange:

  1. For each cluster in ``HELEN_FEDERATED_CLUSTERS``:
     a. Pick one known peer from that cluster.
     b. POST signed (with the cluster's secret) request to
        ``/api/cluster/cross-cluster/sync`` containing our peer
        summary + blocklist hash.
     c. Receive their summary + blocklist hash.
  2. Merge new peer ids into ``services.peer_registry`` (read-only;
     they can't auto-promote without a handshake).
  3. Union both blocklists so a quarantined peer in cluster_b also
     gets blocked locally.

Design intent: federation stays loose-coupled. We never write
shared state to disk on behalf of another cluster; only mirror
their identity blocklist for safety.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


GOSSIP_INTERVAL_SEC = 120.0


_loop_task: Optional[asyncio.Task] = None
_running = False
_stats = {"cycles": 0, "peers_learned": 0, "blocklist_merged": 0}


async def _gossip_one(cluster_id: str, peer_host: str,
                      peer_port: int, secret: str) -> dict:
    try:
        import httpx
        from app.core.federation_auth import sign_request
    except ImportError:
        return {"ok": False, "error": "deps_missing"}

    body = {"peer_summary": _local_peer_summary(),
            "blocklist":     _local_blocklist()}
    body_bytes = json.dumps(body).encode()
    path = "/api/cluster/cross-cluster/sync"
    try:
        headers = sign_request("POST", path, body_bytes, secret=secret)
    except Exception as e:
        return {"ok": False, "error": f"sign_failed:{e}"}
    headers["Content-Type"] = "application/json"

    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(
                f"http://{peer_host}:{peer_port}{path}",
                content=body_bytes, headers=headers,
            )
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code}
        return {"ok": True, "data": r.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def _local_peer_summary() -> list[dict]:
    try:
        from app.services.node_registry import get_registry
        return [
            {
                "node_id": n.node_id,
                "host":    n.host,
                "port":    n.port,
                "fresh":   n.is_fresh(),
            }
            for n in get_registry().nodes(include_dead=False)[:200]
        ]
    except Exception:
        return []


def _local_blocklist() -> list[str]:
    try:
        from app.services.sync_policy import get_sync_policy
        return list(get_sync_policy().snapshot().get("blocked_server_ids", []))
    except Exception:
        return []


def _merge_remote(remote: dict) -> dict:
    """Apply a remote response — count newly learned peers + blocked ids."""
    learned = 0
    merged = 0
    try:
        from app.services.sync_policy import get_sync_policy
        for sid in (remote.get("blocklist") or []):
            if not sid:
                continue
            policy = get_sync_policy()
            if not policy.is_blocked(sid):
                policy.block(sid)
                merged += 1
    except Exception:
        pass
    # Peer summary is informational only (no auto-promote).
    learned = len(remote.get("peer_summary") or [])
    return {"peers_seen": learned, "blocklist_added": merged}


async def gossip_once() -> dict:
    """One cycle across every configured federated cluster."""
    try:
        from app.services.federation_gateway import _parse_cluster_secrets
        from app.services.node_registry import get_registry
    except ImportError as e:
        return {"ok": False, "error": str(e)}

    clusters = _parse_cluster_secrets()
    if not clusters:
        return {"ok": True, "skipped": "no_federated_clusters"}

    reg = get_registry()
    out: dict[str, dict] = {}
    for cid, secret in clusters.items():
        peer = next(
            (n for n in reg.nodes(include_dead=False)
             if not n.self_node and n.host),
            None,
        )
        if peer is None:
            out[cid] = {"ok": False, "error": "no_peer"}
            continue
        result = await _gossip_one(cid, peer.host, peer.port, secret)
        if result.get("ok"):
            stats = _merge_remote(result.get("data") or {})
            _stats["peers_learned"] += stats["peers_seen"]
            _stats["blocklist_merged"] += stats["blocklist_added"]
            out[cid] = {"ok": True, **stats}
        else:
            out[cid] = result
    _stats["cycles"] += 1
    return {"ok": True, "results": out}


async def _run_loop() -> None:
    global _running
    _running = True
    logger.info("cross_cluster_gossip_started",
                interval_sec=GOSSIP_INTERVAL_SEC)
    try:
        while _running:
            try:
                await gossip_once()
            except Exception as e:
                logger.warning("cross_cluster_gossip_cycle_failed",
                               error=str(e))
            await asyncio.sleep(GOSSIP_INTERVAL_SEC)
    finally:
        logger.info("cross_cluster_gossip_stopped")


def start() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _loop_task = loop.create_task(_run_loop(), name="cross-cluster-gossip")
    except RuntimeError:
        logger.warning("cross_cluster_gossip_no_event_loop_yet")


def stop() -> None:
    global _running, _loop_task
    _running = False
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


def status() -> dict:
    return {
        "running":      _running,
        "interval_sec": GOSSIP_INTERVAL_SEC,
        "stats":        dict(_stats),
    }


# Used by the inbound endpoint to build a reply for the remote peer.
def serve_request(remote: dict) -> dict:
    return {
        "peer_summary": _local_peer_summary(),
        "blocklist":    _local_blocklist(),
        "ts":           time.time(),
    }
