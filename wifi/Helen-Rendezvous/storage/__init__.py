"""
Helen-Rendezvous — storage backend package.

Provides a pluggable storage abstraction for tunnel registrations, signaling
state, distributed locks, and cross-instance pub/sub. The default backend is
in-memory (single-process), suitable for the reference deployment. The Redis
backend enables horizontal scale across multiple Rendezvous instances behind a
load balancer.

Public surface:
    StorageBackend     — Protocol that every backend must satisfy.
    MemoryBackend      — single-process in-memory implementation.
    RedisBackend       — Redis (standalone / sentinel / cluster) implementation.
    build_backend()    — factory that reads env vars and returns the right one.
"""

from .backend import StorageBackend  # noqa: F401
from .factory import build_backend  # noqa: F401
from .memory_backend import MemoryBackend  # noqa: F401

try:  # Redis is optional — only required when HELEN_RENDEZVOUS_STORAGE=redis*.
    from .redis_backend import RedisBackend  # noqa: F401
except ImportError:  # pragma: no cover
    RedisBackend = None  # type: ignore[assignment, misc]

__all__ = [
    "StorageBackend",
    "MemoryBackend",
    "RedisBackend",
    "build_backend",
]
