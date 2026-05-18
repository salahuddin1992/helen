"""Node identity — server_id + cluster_id resolution.

Wraps ``services.discovery_service.get_server_id`` so the
distributed_system layer never imports the lower-level service
directly.
"""

from __future__ import annotations

from typing import Optional


def server_id() -> str:
    try:
        from app.services.discovery_service import get_server_id
        return get_server_id() or "anon"
    except Exception:
        return "anon"


def cluster_id() -> str:
    try:
        from app.core.config import get_settings
        return get_settings().COMMCLIENT_CLUSTER_ID or "default"
    except Exception:
        return "default"


def host_port() -> tuple[str, int]:
    try:
        import os, socket
        host = socket.gethostname()
        port = int(os.environ.get("PORT", 3000))
        return host, port
    except Exception:
        return "localhost", 3000


def identity_snapshot() -> dict:
    h, p = host_port()
    return {
        "server_id":  server_id(),
        "cluster_id": cluster_id(),
        "host":       h,
        "port":       p,
    }
