"""Parallel relay — fan a request out to top-K proxies simultaneously.

Sequential failover (the default in ``cluster_mesh.relay_request``)
is bandwidth-frugal but slow when the first proxy is slow. For
latency-sensitive operations we trade bandwidth for speed: dispatch
to top-K proxies, return the first 2xx response, cancel the rest.

Use case: cross-cluster RPCs, time-bound presence updates, mute/kick.

Heuristic: K=3 by default. Larger K = better worst-case latency but
multiplied bandwidth.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


async def _send_via_proxy(
    proxy_node,
    target_node_id: str,
    method: str,
    path: str,
    body: Any,
    timeout: float,
) -> tuple[int, Any, dict, str]:
    """Send a single relay request through one proxy.

    Returns (status, body, headers, proxy_id) — same shape as
    relay_request plus the proxy id for the winner trace.
    """
    try:
        import httpx
    except ImportError:
        return 503, {"error": "httpx_missing"}, {}, proxy_node.node_id

    proxy_url = (f"http://{proxy_node.host}:{proxy_node.port}"
                 f"/api/cluster/relay")
    payload = {
        "target_node_id": target_node_id,
        "method": method,
        "path": path,
        "body": body,
        "_hops_remaining": 3,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(proxy_url, json=payload)
        if r.status_code == 200:
            d = r.json() or {}
            inner_status = int(d.get("status") or 502)
            inner_body = d.get("body")
            return inner_status, inner_body, dict(r.headers), proxy_node.node_id
        return r.status_code, r.text, dict(r.headers), proxy_node.node_id
    except Exception as e:
        return 502, {"error": str(e)[:80]}, {}, proxy_node.node_id


async def dispatch(
    target_node_id: str,
    method: str = "GET",
    path: str = "/",
    body: Any = None,
    *,
    k: int = 3,
    timeout: float = 5.0,
) -> dict:
    """Send to top-K proxies simultaneously; return first 2xx winner.

    Returns a result dict::

        {
          "ok":        bool,
          "status":    int,
          "body":      Any,
          "winner":    "<proxy_node_id>",
          "elapsed_ms": float,
          "attempted": int,
        }
    """
    started = time.time()
    try:
        from app.services.node_registry import get_registry
        from app.services.load_balancer import rank_proxies
    except ImportError as e:
        return {"ok": False, "error": f"deps_missing:{e}"}

    reg = get_registry()
    candidates = [
        n for n in reg.nodes(include_dead=False)
        if not n.self_node and n.node_id != target_node_id
    ]
    if not candidates:
        return {"ok": False, "error": "no_candidates",
                "elapsed_ms": 0}

    try:
        ranked = rank_proxies(candidates, top_k=k)
        proxies = [s.node for s in ranked]
    except Exception:
        proxies = candidates[:k]

    if not proxies:
        return {"ok": False, "error": "no_ranked", "elapsed_ms": 0}

    loop = asyncio.get_event_loop()
    tasks = [
        loop.create_task(
            _send_via_proxy(
                p, target_node_id, method, path, body, timeout,
            ),
            name=f"par-relay-{p.node_id[:8]}",
        )
        for p in proxies
    ]
    attempted = len(tasks)

    winner_status: int | None = None
    winner_body: Any = None
    winner_id: str = ""
    pending = set(tasks)
    deadline = started + timeout

    while pending and winner_status is None:
        wait_for = max(0.0, deadline - time.time())
        if wait_for <= 0:
            break
        done, pending = await asyncio.wait(
            pending, timeout=wait_for,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            try:
                status, body_resp, _headers, proxy_id = t.result()
            except Exception as e:
                logger.debug("parallel_relay_task_raised",
                             error=str(e)[:80])
                continue
            if 200 <= status < 300:
                winner_status, winner_body, winner_id = status, body_resp, proxy_id
                break

    # Cancel remaining tasks once winner is chosen.
    for t in pending:
        t.cancel()

    elapsed_ms = round((time.time() - started) * 1000.0, 2)
    if winner_status is not None:
        return {
            "ok":         True,
            "status":     winner_status,
            "body":       winner_body,
            "winner":     winner_id,
            "elapsed_ms": elapsed_ms,
            "attempted":  attempted,
        }
    return {
        "ok":         False,
        "error":      "all_proxies_failed",
        "elapsed_ms": elapsed_ms,
        "attempted":  attempted,
    }
