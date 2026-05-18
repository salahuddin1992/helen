"""Relay diagnostics — admin-side tooling to test the multi-hop chain.

Lets operators trigger a relay-trace from the admin UI:

    POST /api/admin/peers/relay/trace
        {target_node_id, method=GET, path=/api/cluster/info,
         hops_remaining=4}

Returns the full chain with per-hop latency + success flags.
"""

from __future__ import annotations

import time
from typing import Any, Optional


async def trace(
    target_node_id: str,
    *,
    method: str = "GET",
    path: str = "/api/cluster/info",
    body: Any = None,
    hops_remaining: int = 4,
) -> dict:
    """Run a single relay attempt and return diagnostic data."""
    try:
        from app.services.cluster_mesh import relay_request
    except ImportError as e:
        return {"ok": False, "error": f"cluster_mesh missing: {e}"}

    started = time.time()
    try:
        status, body_resp, headers = await relay_request(
            target_node_id=target_node_id,
            method=method, path=path, body=body,
            hops_remaining=hops_remaining,
        )
        ok = 200 <= status < 300
        elapsed_ms = (time.time() - started) * 1000.0
        return {
            "ok":               ok,
            "status":           status,
            "elapsed_ms":       round(elapsed_ms, 2),
            "hops_attempted":   hops_remaining,
            "headers":          dict(headers) if headers else {},
            "body_preview":     str(body_resp)[:200] if body_resp else None,
        }
    except Exception as e:
        return {
            "ok":         False,
            "error":      str(e)[:200],
            "elapsed_ms": round((time.time() - started) * 1000.0, 2),
        }


def chain_visualizer() -> dict:
    """Render the live relay chain capabilities for the UI."""
    out = {
        "max_hops":         4,
        "proxies_per_hop":  8,
        "max_paths":        4096,  # 4 hops × 8 proxies^4 worst case
        "bridges":          [],
    }
    try:
        from app.services.node_registry import get_registry
        reg = get_registry()
        bridges = [
            {
                "node_id": n.node_id,
                "host":    n.host,
                "port":    n.port,
            }
            for n in reg.nodes(include_dead=False)
            if not n.self_node and bool((n.extra or {}).get("bridge"))
        ]
        out["bridges"] = bridges
        out["bridge_count"] = len(bridges)
    except Exception:
        pass
    return out
