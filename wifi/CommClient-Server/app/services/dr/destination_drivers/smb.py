"""SMB / CIFS driver — uses ``smbprotocol`` if available, else falls back
to a mounted UNC path on Windows (``\\\\host\\share``)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List

from app.core.logging import get_logger

from .base import DRDestinationDriver, DriverHealth, DriverWriteResult


logger = get_logger(__name__)


try:  # pragma: no cover — optional dep
    import smbclient  # type: ignore
    _SMB_OK = True
except Exception:
    smbclient = None  # type: ignore
    _SMB_OK = False


class SMBDriver(DRDestinationDriver):
    kind = "smb"

    def __init__(
        self,
        server: str,
        share: str,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
        mounted_path: str | None = None,
        **_: Any,
    ) -> None:
        self.server = server
        self.share = share
        self.username = username
        self.password = password
        self.domain = domain
        self.mounted_path = mounted_path
        self._native = bool(_SMB_OK and not mounted_path)
        if self._native and _SMB_OK:
            smbclient.register_session(  # type: ignore[union-attr]
                server, username=username, password=password,
            )

    # ── helpers ─────────────────────────────────────────────────────

    def _remote(self, prefix: str, seq: int | None = None) -> str:
        prefix = prefix.strip("/").replace("..", "_")
        if self.mounted_path:
            base = Path(self.mounted_path) / prefix
            base.mkdir(parents=True, exist_ok=True)
            if seq is None:
                return str(base)
            return str(base / f"chunk_{seq:08d}.bin")
        _bs = "\\"
        _prefix_win = prefix.replace("/", _bs)
        base = f"\\\\{self.server}\\{self.share}\\{_prefix_win}"
        if seq is None:
            return base
        return f"{base}\\chunk_{seq:08d}.bin"

    # ── interface ────────────────────────────────────────────────────

    async def write_chunk(
        self, prefix: str, seq: int, data: bytes, *, sha256: str,
    ) -> DriverWriteResult:
        t0 = time.perf_counter()
        target = self._remote(prefix, seq)
        if self.mounted_path:
            await asyncio.to_thread(Path(target).write_bytes, data)
        elif _SMB_OK:
            def _w():
                with smbclient.open_file(target, mode="wb") as f:  # type: ignore[union-attr]
                    f.write(data)
            await asyncio.to_thread(_w)
        else:
            raise RuntimeError("smbprotocol/smbclient not installed and no mounted_path")
        dt = (time.perf_counter() - t0) * 1000.0
        return DriverWriteResult(
            storage_key=target, bytes_written=len(data),
            sha256=sha256, duration_ms=dt, encrypted_size=len(data),
        )

    async def read_chunk(self, prefix: str, seq: int) -> bytes:
        target = self._remote(prefix, seq)
        if self.mounted_path:
            return await asyncio.to_thread(Path(target).read_bytes)
        if _SMB_OK:
            def _r() -> bytes:
                with smbclient.open_file(target, mode="rb") as f:  # type: ignore[union-attr]
                    return f.read()
            return await asyncio.to_thread(_r)
        raise RuntimeError("smbprotocol not installed and no mounted_path")

    async def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        base = self._remote(prefix)
        out: List[Dict[str, Any]] = []
        if self.mounted_path:
            p = Path(base)
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        st = f.stat()
                        out.append({"key": f.name, "size": st.st_size,
                                    "mtime": st.st_mtime})
        elif _SMB_OK:
            def _l():
                try:
                    return list(smbclient.scandir(base))  # type: ignore[union-attr]
                except Exception:
                    return []
            entries = await asyncio.to_thread(_l)
            for e in entries:
                try:
                    out.append({"key": e.name, "size": e.stat().st_size,
                                "mtime": e.stat().st_mtime})
                except Exception:
                    continue
        return out

    async def delete(self, prefix: str) -> bool:
        target = self._remote(prefix)
        try:
            if self.mounted_path:
                p = Path(target)
                if p.is_dir():
                    import shutil
                    await asyncio.to_thread(shutil.rmtree, str(p), True)
                    return True
                if p.is_file():
                    await asyncio.to_thread(p.unlink, True)
                    return True
            elif _SMB_OK:
                await asyncio.to_thread(smbclient.remove, target)  # type: ignore[union-attr]
                return True
        except Exception as e:
            logger.warning("smb_delete_failed", target=target, error=str(e))
        return False

    async def capacity(self) -> Dict[str, int]:
        if self.mounted_path:
            import shutil
            st = await asyncio.to_thread(shutil.disk_usage, self.mounted_path)
            return {"capacity_bytes": st.total, "used_bytes": st.used,
                    "free_bytes": st.free}
        return {"capacity_bytes": 0, "used_bytes": 0, "free_bytes": 0}

    async def test(self) -> DriverHealth:
        t0 = time.perf_counter()
        try:
            await self.list_objects("")
            dt = (time.perf_counter() - t0) * 1000.0
            cap = await self.capacity()
            return DriverHealth(
                ok=True, kind=self.kind, latency_ms=dt,
                capacity_bytes=cap["capacity_bytes"],
                used_bytes=cap["used_bytes"],
                free_bytes=cap["free_bytes"],
                details={"server": self.server, "share": self.share,
                         "mounted_path": self.mounted_path,
                         "native": self._native},
            )
        except Exception as e:
            return DriverHealth(ok=False, kind=self.kind, error=str(e))
