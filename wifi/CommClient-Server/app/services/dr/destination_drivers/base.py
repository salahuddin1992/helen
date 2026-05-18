"""
Common DR v2 destination driver interface.

Drivers are deliberately **chunk-oriented** rather than object-oriented:
the backup engine streams AES-256-GCM-encrypted chunks to ``write_chunk``
and the verifier / restorer pulls them back via ``read_chunk``.  This
matches what tape / USB / SMB present better than a "single object per
backup" abstraction.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional


@dataclass
class DriverHealth:
    ok: bool
    kind: str
    latency_ms: float = 0.0
    capacity_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "latency_ms": self.latency_ms,
            "capacity_bytes": self.capacity_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "error": self.error,
            "details": dict(self.details),
        }


@dataclass
class DriverWriteResult:
    storage_key: str
    bytes_written: int
    sha256: str
    duration_ms: float = 0.0
    encrypted_size: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "storage_key": self.storage_key,
            "bytes_written": self.bytes_written,
            "sha256": self.sha256,
            "duration_ms": self.duration_ms,
            "encrypted_size": self.encrypted_size,
        }


class DRDestinationDriver(abc.ABC):
    """Common async interface for every DR v2 destination."""

    kind: str = "abstract"

    # ── lifecycle ───────────────────────────────────────────────────

    async def close(self) -> None:
        """Release any background connections.  Default: no-op."""
        return None

    # ── primary I/O ─────────────────────────────────────────────────

    @abc.abstractmethod
    async def write_chunk(
        self,
        prefix: str,
        seq: int,
        data: bytes,
        *,
        sha256: str,
    ) -> DriverWriteResult: ...

    @abc.abstractmethod
    async def read_chunk(self, prefix: str, seq: int) -> bytes: ...

    @abc.abstractmethod
    async def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    async def delete(self, prefix: str) -> bool: ...

    @abc.abstractmethod
    async def capacity(self) -> Dict[str, int]: ...

    @abc.abstractmethod
    async def test(self) -> DriverHealth: ...

    # ── convenience ─────────────────────────────────────────────────

    async def write_stream(
        self,
        prefix: str,
        chunks: AsyncIterator[bytes],
    ) -> List[DriverWriteResult]:
        """Default implementation — call ``write_chunk`` in sequence."""
        import hashlib

        out: List[DriverWriteResult] = []
        seq = 0
        async for c in chunks:
            h = hashlib.sha256(c).hexdigest()
            res = await self.write_chunk(prefix, seq, c, sha256=h)
            out.append(res)
            seq += 1
        return out
