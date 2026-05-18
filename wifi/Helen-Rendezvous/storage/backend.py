"""
Abstract storage backend interface for Helen-Rendezvous.

Every concrete backend (in-memory, Redis standalone, Redis sentinel, Redis
cluster) implements this Protocol so the rest of the rendezvous treats storage
as a black box. Adding a new backend (etcd, FoundationDB, ScyllaDB, etc.) is a
matter of implementing this interface plus wiring it through factory.py.

Design notes
------------
* All methods are `async`. Memory backends fake async by yielding immediately.
* Methods return primitive types (dict / list / str / bool / None) — never
  framework objects. Keeps the rendezvous core decoupled from any one DB.
* TTL is expressed in seconds for every backend; converters live inside each
  backend if its wire protocol uses something else.
* `acquire_lock` returns an opaque token; pass the same token to `release_lock`
  to guarantee fencing. Implementations MUST verify the token before deleting.
* `publish_event` returns the number of subscribers that received the message.
  For backends that can't count (memory), they return the local fan-out count.
* `subscribe_events` is an async generator. Callers MUST close it.
* `health` returns a stable shape: {"backend": str, "status": "ok|degraded|down",
  "latency_ms": float, "details": dict}.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """The contract every rendezvous storage backend must satisfy."""

    # ── Tunnels ────────────────────────────────────────────

    async def register_tunnel(
        self,
        peer_id: str,
        info: dict[str, Any],
        ttl: int,
    ) -> str:
        """Persist a tunnel registration. Returns the storage key written."""

    async def lookup_tunnel(self, peer_id: str) -> Optional[dict[str, Any]]:
        """Return the stored tunnel info, or None if absent / expired."""

    async def unregister_tunnel(self, peer_id: str) -> bool:
        """Remove a tunnel registration. Returns True if a key was deleted."""

    async def list_tunnels(self) -> list[dict[str, Any]]:
        """Return every active tunnel registration (uses SCAN on Redis)."""

    async def refresh_tunnel(self, peer_id: str, ttl: int) -> bool:
        """Extend a tunnel's TTL without re-writing its body."""

    # ── Signaling ──────────────────────────────────────────

    async def register_signal(
        self,
        key: str,
        payload: dict[str, Any],
        ttl: int,
    ) -> bool:
        """Persist a hole-punch signal entry."""

    async def lookup_signal(self, key: str) -> Optional[dict[str, Any]]:
        """Return the stored signal entry, or None."""

    async def delete_signal(self, key: str) -> bool:
        """Remove a stored signal."""

    async def list_signals(self) -> list[str]:
        """Return every active signal key."""

    # ── Pub/sub ────────────────────────────────────────────

    async def publish_event(self, channel: str, payload: dict[str, Any]) -> int:
        """Publish a JSON-serialisable event. Returns the receiver count."""

    def subscribe_events(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over events. Caller must `aclose()` when done."""

    # ── Distributed locks ──────────────────────────────────

    async def acquire_lock(self, key: str, ttl: int) -> Optional[str]:
        """SET NX EX semantics. Returns an opaque fencing token, or None."""

    async def release_lock(self, key: str, token: str) -> bool:
        """Release a lock IFF the token matches. Returns True on success."""

    # ── Health ─────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        """Backend health probe. Stable shape — see module docstring."""

    # ── Lifecycle ──────────────────────────────────────────

    async def close(self) -> None:
        """Release any connections / background tasks owned by the backend."""
