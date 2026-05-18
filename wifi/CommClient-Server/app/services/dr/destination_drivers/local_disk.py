"""Local disk DR destination driver."""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

from app.core.logging import get_logger

from .base import DRDestinationDriver, DriverHealth, DriverWriteResult


logger = get_logger(__name__)


class LocalDiskDriver(DRDestinationDriver):
    kind = "local-disk"

    def __init__(self, root: str = "data/dr_v2/local", **_: Any) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, prefix: str, seq: int | None = None) -> Path:
        base = self.root / prefix.strip("/").replace("..", "_")
        if seq is None:
            return base
        base.mkdir(parents=True, exist_ok=True)
        return base / f"chunk_{seq:08d}.bin"

    async def write_chunk(
        self, prefix: str, seq: int, data: bytes, *, sha256: str,
    ) -> DriverWriteResult:
        t0 = time.perf_counter()
        p = self._path(prefix, seq)
        await asyncio.to_thread(p.write_bytes, data)
        dt = (time.perf_counter() - t0) * 1000.0
        return DriverWriteResult(
            storage_key=str(p), bytes_written=len(data),
            sha256=sha256, duration_ms=dt, encrypted_size=len(data),
        )

    async def read_chunk(self, prefix: str, seq: int) -> bytes:
        p = self._path(prefix, seq)
        return await asyncio.to_thread(p.read_bytes)

    async def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        base = self._path(prefix) if prefix else self.root
        out: List[Dict[str, Any]] = []
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    st = p.stat()
                    out.append({
                        "key": str(p.relative_to(self.root)),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    })
        return out

    async def delete(self, prefix: str) -> bool:
        p = self._path(prefix)
        if p.is_dir():
            await asyncio.to_thread(shutil.rmtree, str(p), True)
            return True
        if p.is_file():
            await asyncio.to_thread(p.unlink, True)
            return True
        return False

    async def capacity(self) -> Dict[str, int]:
        st = await asyncio.to_thread(shutil.disk_usage, str(self.root))
        return {"capacity_bytes": st.total, "used_bytes": st.used,
                "free_bytes": st.free}

    async def test(self) -> DriverHealth:
        t0 = time.perf_counter()
        try:
            probe = self.root / ".healthcheck"
            payload = b"healthcheck-" + str(time.time()).encode()
            await asyncio.to_thread(probe.write_bytes, payload)
            data = await asyncio.to_thread(probe.read_bytes)
            await asyncio.to_thread(probe.unlink, True)
            dt = (time.perf_counter() - t0) * 1000.0
            ok = data == payload
            cap = await self.capacity()
            return DriverHealth(
                ok=ok, kind=self.kind, latency_ms=dt,
                capacity_bytes=cap["capacity_bytes"],
                used_bytes=cap["used_bytes"],
                free_bytes=cap["free_bytes"],
                details={"root": str(self.root)},
            )
        except Exception as e:
            return DriverHealth(ok=False, kind=self.kind, error=str(e))
