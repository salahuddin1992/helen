"""
LAN-server ASGI entry module (Task #4).

Import `app.lan_server_app:app` instead of `app.main:app` when running
the backend in the "PC = LAN server" topology. This module:

  * Imports the fully-built `app.main.app` (FastAPI + Socket.IO ASGI).
  * Calls `apply_extended_lifespan()` on the inner FastAPI so the
    extended startup/shutdown hooks wrap the original lifespan without
    touching `app/main.py`.
  * Re-exports the same ASGI callable so uvicorn/hypercorn see a
    drop-in replacement.

Usage
-----
Development:
    uvicorn app.lan_server_app:app --host 0.0.0.0 --port 3000

PyInstaller:
    update CommClient.spec entry to `app.lan_server_app` — identical
    bytecode, extra init.

Electron launcher (src/main/serverEnv.ts):
    spawn `python -m uvicorn app.lan_server_app:app --host 0.0.0.0 --port ...`
"""

from __future__ import annotations

from app.core.extended_bootstrap import apply_extended_lifespan
from app.main import app as _combined_app
from app.main import create_app  # re-exported for tooling
from app.main import create_combined_app  # re-exported for tooling


def _apply_to_inner_fastapi(combined: object) -> None:
    """
    `create_combined_app()` returns a `socketio.ASGIApp` wrapping the
    FastAPI app. The lifespan lives on the FastAPI side (`.router.lifespan_context`),
    so we need to reach through the Socket.IO wrapper to find it.
    """
    # socketio.ASGIApp exposes the inner app via `other_asgi_app`.
    inner = getattr(combined, "other_asgi_app", None)
    if inner is None:
        # Already a FastAPI app (or unknown shape). Apply directly.
        apply_extended_lifespan(combined)
        return
    apply_extended_lifespan(inner)


_apply_to_inner_fastapi(_combined_app)

# The ASGI app uvicorn imports.
app = _combined_app

__all__ = ["app", "create_app", "create_combined_app"]
