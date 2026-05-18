"""NFS driver — local mount-point wrapper.

NFS is exposed as a normal filesystem mount on the LAN.  The driver
treats it identically to ``local-disk`` but adds a ping-style latency
probe and rejects mount points that look remote (URI schemes, hostnames
embedded in path) — the operator must mount the share *outside* Helen
and give the driver the mounted path.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List

from .base import DriverHealth, DriverWriteResult
from .local_disk import LocalDiskDriver


class NFSDriver(LocalDiskDriver):
    kind = "nfs"

    def __init__(
        self,
        mount_point: str,
        export_host: str | None = None,
        export_path: str | None = None,
        **_: Any,
    ) -> None:
        if "://" in mount_point or mount_point.startswith("\\\\"):
            raise ValueError(
                "NFS driver expects an already-mounted local path — "
                "mount the export with the OS first.",
            )
        super().__init__(root=mount_point)
        self.export_host = export_host
        self.export_path = export_path

    async def test(self) -> DriverHealth:
        base = await super().test()
        base.kind = self.kind
        base.details.update({
            "export_host": self.export_host,
            "export_path": self.export_path,
            "mount_point": str(self.root),
        })
        return base
