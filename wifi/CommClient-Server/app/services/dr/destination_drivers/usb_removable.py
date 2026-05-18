"""USB removable-media driver — best-effort.

Treats a mount point as a normal disk but adds presence + ejection
metadata.  In production this should integrate with udev / WMI to
detect plug events, but for now we just check the mount point exists.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .base import DriverHealth
from .local_disk import LocalDiskDriver


class USBRemovableDriver(LocalDiskDriver):
    kind = "usb-removable"

    def __init__(
        self,
        mount_point: str,
        device_id: str | None = None,
        label: str | None = None,
        require_label: bool = False,
        **_: Any,
    ) -> None:
        super().__init__(root=mount_point)
        self.mount_point = mount_point
        self.device_id = device_id
        self.label = label
        self.require_label = require_label

    async def test(self) -> DriverHealth:
        h = await super().test()
        h.kind = self.kind
        present = Path(self.mount_point).exists()
        if not present:
            h.ok = False
            h.error = (h.error or "") + f"; mount point {self.mount_point} not present"
        h.details.update({
            "mount_point": self.mount_point,
            "device_id": self.device_id,
            "label": self.label,
            "present": present,
            "warning": "USB driver is best-effort — no hot-plug detection.",
        })
        return h
