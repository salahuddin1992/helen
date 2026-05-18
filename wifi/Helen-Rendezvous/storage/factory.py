"""
Storage backend factory.

Reads env vars and returns the right concrete backend. Falls back to
`MemoryBackend` when nothing is configured — that's the documented default
so existing single-instance deployments keep working with zero changes.

Env vars:
    HELEN_RENDEZVOUS_STORAGE                memory | redis | redis-sentinel | redis-cluster
    HELEN_RENDEZVOUS_REDIS_URL              e.g. redis://:secret@host:6379/0
    HELEN_RENDEZVOUS_REDIS_USERNAME         optional Redis ACL username
    HELEN_RENDEZVOUS_REDIS_PASSWORD         optional Redis password
    HELEN_RENDEZVOUS_REDIS_TLS              "1" forces ssl_cert_reqs=required
    HELEN_RENDEZVOUS_REDIS_SENTINELS        host:port,host:port,...
    HELEN_RENDEZVOUS_REDIS_SENTINEL_MASTER  master name (default "mymaster")
    HELEN_RENDEZVOUS_REDIS_SENTINEL_PASSWORD  sentinel auth (optional)
    HELEN_RENDEZVOUS_REDIS_CLUSTER_NODES    host:port,host:port,...
    HELEN_RENDEZVOUS_REDIS_KEY_PREFIX       string prefix for every key
    HELEN_RENDEZVOUS_REDIS_EVENTS_CHANNEL   defaults to "rendezvous:events"
"""

from __future__ import annotations

import os
from typing import Any, Optional

import structlog

from .backend import StorageBackend
from .memory_backend import MemoryBackend

logger = structlog.get_logger(__name__)


def _parse_hostports(spec: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            host, port_s = token.rsplit(":", 1)
            try:
                out.append((host.strip(), int(port_s.strip())))
            except ValueError:
                continue
        else:
            out.append((token, 6379))
    return out


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_backend(config: Optional[dict[str, Any]] = None) -> StorageBackend:
    """Build the storage backend selected by env / config dict.

    Pass `config` to override env (test injection). Keys mirror env names but
    are case-insensitive.
    """
    cfg = {k.upper(): v for k, v in (config or {}).items()}

    def _get(key: str, default: Optional[str] = None) -> Optional[str]:
        if key in cfg:
            v = cfg[key]
            return None if v is None else str(v)
        return os.environ.get(key, default)

    mode = (_get("HELEN_RENDEZVOUS_STORAGE", "memory") or "memory").strip().lower()
    key_prefix = _get("HELEN_RENDEZVOUS_REDIS_KEY_PREFIX", "") or ""
    events_channel = (
        _get("HELEN_RENDEZVOUS_REDIS_EVENTS_CHANNEL", "rendezvous:events")
        or "rendezvous:events"
    )

    if mode in ("memory", "inmemory", "in-memory"):
        logger.info("storage_backend_selected", mode="memory")
        return MemoryBackend()

    # All remaining modes require redis-py.
    try:
        from .redis_backend import RedisBackend, RedisUnavailable  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        logger.error(
            "storage_backend_unavailable",
            mode=mode,
            error=str(exc),
            fallback="memory",
        )
        return MemoryBackend()

    password = _get("HELEN_RENDEZVOUS_REDIS_PASSWORD") or None
    username = _get("HELEN_RENDEZVOUS_REDIS_USERNAME") or None
    tls = _env_bool("HELEN_RENDEZVOUS_REDIS_TLS", default=False)

    if mode in ("redis", "redis-standalone", "standalone"):
        url = _get("HELEN_RENDEZVOUS_REDIS_URL", "redis://localhost:6379/0") \
            or "redis://localhost:6379/0"
        logger.info("storage_backend_selected", mode="redis", topology="standalone")
        return RedisBackend.from_url(
            url,
            events_channel=events_channel,
            key_prefix=key_prefix,
            password=password,
            username=username,
            ssl_cert_reqs="required" if tls else None,
        )

    if mode in ("redis-sentinel", "sentinel"):
        spec = _get("HELEN_RENDEZVOUS_REDIS_SENTINELS", "") or ""
        sentinels = _parse_hostports(spec)
        master = _get("HELEN_RENDEZVOUS_REDIS_SENTINEL_MASTER", "mymaster") or "mymaster"
        sent_pw = _get("HELEN_RENDEZVOUS_REDIS_SENTINEL_PASSWORD") or None
        if not sentinels:
            logger.error("redis_sentinel_missing_nodes", fallback="memory")
            return MemoryBackend()
        logger.info(
            "storage_backend_selected",
            mode="redis",
            topology="sentinel",
            master=master,
            sentinels=len(sentinels),
        )
        return RedisBackend.from_sentinels(
            sentinels,
            master,
            password=password,
            sentinel_password=sent_pw,
            events_channel=events_channel,
            key_prefix=key_prefix,
            ssl=tls,
        )

    if mode in ("redis-cluster", "cluster"):
        spec = _get("HELEN_RENDEZVOUS_REDIS_CLUSTER_NODES", "") or ""
        nodes = _parse_hostports(spec)
        if not nodes:
            logger.error("redis_cluster_missing_nodes", fallback="memory")
            return MemoryBackend()
        logger.info(
            "storage_backend_selected",
            mode="redis",
            topology="cluster",
            nodes=len(nodes),
        )
        return RedisBackend.from_cluster(
            nodes,
            password=password,
            username=username,
            events_channel=events_channel,
            key_prefix=key_prefix,
            ssl=tls,
        )

    logger.warning("storage_backend_unknown_mode", mode=mode, fallback="memory")
    return MemoryBackend()
