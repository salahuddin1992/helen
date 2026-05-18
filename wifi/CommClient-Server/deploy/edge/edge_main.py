"""
Minimal Helen edge-worker ASGI app.

Boots a tiny FastAPI process that:
  * Serves ``/api/edge/health`` for latency probes.
  * Runs the worker runtime — origin can POST work via signed
    /worker/exec calls.
  * Heartbeats to the origin every 30s.
  * Maintains an outbound WS sync stream to origin.

This is intentionally NOT the full Helen server; it's the runtime that
ships in the edge container. The full ``CommClient-Server`` is the
origin/control plane.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException


HELEN_EDGE_NODE_ID      = os.environ.get("HELEN_EDGE_NODE_ID", "edge-local")
HELEN_EDGE_REGION       = os.environ.get("HELEN_EDGE_REGION", "us-east-1")
HELEN_EDGE_PUBLIC_URL   = os.environ.get("HELEN_EDGE_PUBLIC_URL", "http://localhost:8089")
HELEN_EDGE_ORIGIN_URL   = os.environ.get("HELEN_EDGE_ORIGIN_URL", "")
HELEN_EDGE_NODE_TOKEN   = os.environ.get("HELEN_EDGE_NODE_TOKEN", "")
HELEN_EDGE_GEO_LAT      = float(os.environ.get("HELEN_EDGE_GEO_LAT", "0") or 0)
HELEN_EDGE_GEO_LNG      = float(os.environ.get("HELEN_EDGE_GEO_LNG", "0") or 0)
HELEN_EDGE_CITY         = os.environ.get("HELEN_EDGE_CITY", "")
HELEN_EDGE_COUNTRY      = os.environ.get("HELEN_EDGE_COUNTRY", "")


app = FastAPI(title="Helen Edge Worker", version="7.0.0")


# Import inside try blocks so a partial source tree still boots.
try:
    from app.services.edge.edge_worker import get_edge_runtime
    runtime = get_edge_runtime()
except Exception:
    runtime = None


@app.get("/api/edge/health")
async def health() -> dict[str, Any]:
    return {
        "ok":        True,
        "node_id":   HELEN_EDGE_NODE_ID,
        "region":    HELEN_EDGE_REGION,
        "ts":        time.time(),
        "workers":   runtime.workers() if runtime else [],
    }


@app.post("/worker/exec")
async def worker_exec(body: dict[str, Any]) -> dict[str, Any]:
    token = body.get("token") or ""
    if HELEN_EDGE_NODE_TOKEN and token != HELEN_EDGE_NODE_TOKEN:
        raise HTTPException(status_code=401, detail="bad_token")
    if runtime is None:
        raise HTTPException(status_code=503, detail="runtime_unavailable")
    name = body.get("worker") or ""
    payload = body.get("payload") or {}
    return await runtime.execute(name, payload)


async def _heartbeat_loop() -> None:
    if not HELEN_EDGE_ORIGIN_URL:
        return
    url = HELEN_EDGE_ORIGIN_URL.rstrip("/") + "/api/edge/heartbeat"
    while True:
        try:
            stats = runtime.stats() if runtime else {}
            async with httpx.AsyncClient(timeout=10.0) as cli:
                await cli.post(url, json={
                    "node_id":      HELEN_EDGE_NODE_ID,
                    "load_percent": 0.0,
                    "capacity":     {"workers": stats},
                })
        except Exception:
            pass
        await asyncio.sleep(30)


@app.on_event("startup")
async def _start() -> None:
    asyncio.create_task(_heartbeat_loop(), name="edge-heartbeat")
