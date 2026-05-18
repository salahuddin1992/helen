"""
Helen-Rendezvous admin / cluster diagnostics endpoints.

All routes are mounted under `/admin/cluster/*` and are *read-only*. They use
the same bootstrap token as the rest of the rendezvous (Authorization header
or `?token=...` query param). Operators typically scrape these from a
Prometheus exporter sidecar or a small status-page dashboard.

Endpoints:
    GET /admin/cluster/instances    list of all live rendezvous instances
    GET /admin/cluster/stats        aggregate tunnel / signal counts
    GET /admin/cluster/health       per-component health (backend + cluster)
    GET /admin/cluster/tunnels      paginated shared tunnel index
    GET /admin/cluster/relay/stats  cross-instance pub/sub counters
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request


def register_admin_routes(app: FastAPI) -> None:
    """Attach the admin router to an existing FastAPI app."""

    def _auth(request: Request) -> None:
        # Defer the actual token check to main's _require_token so behaviour
        # tracks the rest of the service exactly.
        import main as _m
        token = request.query_params.get("token")
        _m._require_token(request.headers.get("Authorization"), token)

    @app.get("/admin/cluster/instances")
    async def list_instances(request: Request) -> dict[str, Any]:
        _auth(request)
        import main as _m
        if _m.instance_registry is None:
            return {"cluster_enabled": False, "instances": []}
        instances = await _m.instance_registry.list_active_instances()
        return {
            "cluster_enabled": True,
            "self_instance_id": _m.instance_registry.instance_id,
            "instance_count": len(instances),
            "instances": instances,
        }

    @app.get("/admin/cluster/stats")
    async def cluster_stats(request: Request) -> dict[str, Any]:
        _auth(request)
        import main as _m
        local_tunnels = len(_m.tunnels)
        local_signals = len(_m.signals)
        shared_tunnels: list[dict[str, Any]] = []
        if _m.backend is not None:
            try:
                shared_tunnels = await _m.backend.list_tunnels()
            except Exception:
                shared_tunnels = []
        return {
            "ts": time.time(),
            "local": {
                "tunnels": local_tunnels,
                "signals": local_signals,
                "inflight_total": sum(len(t.inflight) for t in _m.tunnels.values()),
            },
            "cluster": {
                "tunnels": len(shared_tunnels),
                "instances": (
                    await _m.instance_registry.count_active_instances()
                    if _m.instance_registry is not None else 1
                ),
            },
        }

    @app.get("/admin/cluster/health")
    async def cluster_health(request: Request) -> dict[str, Any]:
        _auth(request)
        import main as _m
        out: dict[str, Any] = {
            "service": "Helen-Rendezvous",
            "version": _m.INSTANCE_VERSION,
            "instance_id": (
                _m.instance_registry.instance_id
                if _m.instance_registry is not None else None
            ),
            "backend": None,
            "cluster": None,
        }
        if _m.backend is not None:
            try:
                out["backend"] = await _m.backend.health()
            except Exception as exc:
                out["backend"] = {"status": "down", "error": str(exc)}
        if _m.cross_instance is not None:
            out["cluster"] = {
                "relay_stats": _m.cross_instance.stats,
                "channel": _m.cross_instance.channel,
            }
        overall = "ok"
        if out.get("backend") and out["backend"].get("status") != "ok":
            overall = out["backend"]["status"]
        out["status"] = overall
        return out

    @app.get("/admin/cluster/tunnels")
    async def cluster_tunnels(
        request: Request,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        _auth(request)
        import main as _m
        if _m.backend is None:
            raise HTTPException(status_code=503, detail="cluster mode not enabled")
        all_tunnels = await _m.backend.list_tunnels()
        sliced = all_tunnels[offset : offset + max(1, limit)]
        return {
            "total": len(all_tunnels),
            "offset": offset,
            "limit": limit,
            "items": sliced,
        }

    @app.get("/admin/cluster/relay/stats")
    async def relay_stats(request: Request) -> dict[str, Any]:
        _auth(request)
        import main as _m
        if _m.cross_instance is None:
            raise HTTPException(status_code=503, detail="cross-instance relay not running")
        return {
            "instance_id": _m.cross_instance.instance_id,
            "channel": _m.cross_instance.channel,
            "stats": _m.cross_instance.stats,
        }
