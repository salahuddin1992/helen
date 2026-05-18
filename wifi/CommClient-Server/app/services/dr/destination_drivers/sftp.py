"""SFTP driver (paramiko-backed) — LAN-only.

Hostnames are not validated against a public/private split here because
SFTP is often used over a tunneled / NAT-ed LAN.  The operator is
responsible for choosing a LAN target.  AWS endpoints are still blocked
by the MinIO driver — this driver is generic SSH.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

from app.core.logging import get_logger

from .base import DRDestinationDriver, DriverHealth, DriverWriteResult


logger = get_logger(__name__)


try:  # pragma: no cover — optional dep
    import paramiko  # type: ignore
    _PARAMIKO_OK = True
except Exception:
    paramiko = None  # type: ignore
    _PARAMIKO_OK = False


class SFTPDriver(DRDestinationDriver):
    kind = "sftp"

    def __init__(
        self,
        host: str,
        user: str,
        port: int = 22,
        password: str | None = None,
        key_path: str | None = None,
        remote_root: str = "/backups",
        **_: Any,
    ) -> None:
        if not _PARAMIKO_OK:
            raise RuntimeError("paramiko not installed — SFTP destination unavailable")
        self.host = host
        self.user = user
        self.port = int(port)
        self.password = password
        self.key_path = key_path
        self.remote_root = remote_root.rstrip("/") or "/backups"

    def _key(self, prefix: str, seq: int | None = None) -> str:
        prefix = prefix.strip("/").replace("..", "_")
        base = f"{self.remote_root}/{prefix}"
        if seq is None:
            return base
        return f"{base}/chunk_{seq:08d}.bin"

    def _connect(self):
        t = paramiko.Transport((self.host, self.port))  # type: ignore[attr-defined]
        if self.key_path:
            pkey = paramiko.RSAKey.from_private_key_file(self.key_path)  # type: ignore[attr-defined]
            t.connect(username=self.user, pkey=pkey)
        else:
            t.connect(username=self.user, password=self.password)
        return paramiko.SFTPClient.from_transport(t), t  # type: ignore[attr-defined]

    def _mkdir_p(self, sftp, path: str) -> None:
        parts = path.split("/")
        cur = ""
        for part in parts[:-1]:
            if not part:
                continue
            cur += "/" + part
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError:
                    pass

    async def write_chunk(
        self, prefix: str, seq: int, data: bytes, *, sha256: str,
    ) -> DriverWriteResult:
        t0 = time.perf_counter()
        target = self._key(prefix, seq)

        def _do():
            sftp, t = self._connect()
            try:
                self._mkdir_p(sftp, target)
                with sftp.open(target, "wb") as f:
                    f.write(data)
            finally:
                sftp.close(); t.close()
        await asyncio.to_thread(_do)
        dt = (time.perf_counter() - t0) * 1000.0
        return DriverWriteResult(
            storage_key=target, bytes_written=len(data),
            sha256=sha256, duration_ms=dt, encrypted_size=len(data),
        )

    async def read_chunk(self, prefix: str, seq: int) -> bytes:
        target = self._key(prefix, seq)
        def _do() -> bytes:
            sftp, t = self._connect()
            try:
                with sftp.open(target, "rb") as f:
                    return f.read()
            finally:
                sftp.close(); t.close()
        return await asyncio.to_thread(_do)

    async def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        root = self._key(prefix or ".")
        def _do():
            sftp, t = self._connect()
            try:
                out: List[Dict[str, Any]] = []
                try:
                    for attr in sftp.listdir_attr(root):
                        out.append({"key": attr.filename,
                                    "size": attr.st_size or 0,
                                    "mtime": attr.st_mtime or 0})
                except IOError:
                    pass
                return out
            finally:
                sftp.close(); t.close()
        return await asyncio.to_thread(_do)

    async def delete(self, prefix: str) -> bool:
        target = self._key(prefix)
        def _do():
            sftp, t = self._connect()
            try:
                try:
                    sftp.remove(target); return True
                except IOError:
                    try:
                        for f in sftp.listdir(target):
                            try:
                                sftp.remove(f"{target}/{f}")
                            except Exception:
                                pass
                        sftp.rmdir(target); return True
                    except IOError:
                        return False
            finally:
                sftp.close(); t.close()
        return await asyncio.to_thread(_do)

    async def capacity(self) -> Dict[str, int]:
        # remote disk usage isn't reliably reachable over plain SFTP; best-effort.
        return {"capacity_bytes": 0, "used_bytes": 0, "free_bytes": 0}

    async def test(self) -> DriverHealth:
        t0 = time.perf_counter()
        try:
            def _ping():
                sftp, t = self._connect()
                try:
                    return True
                finally:
                    sftp.close(); t.close()
            await asyncio.to_thread(_ping)
            dt = (time.perf_counter() - t0) * 1000.0
            return DriverHealth(
                ok=True, kind=self.kind, latency_ms=dt,
                details={"host": self.host, "remote_root": self.remote_root},
            )
        except Exception as e:
            return DriverHealth(ok=False, kind=self.kind, error=str(e))
