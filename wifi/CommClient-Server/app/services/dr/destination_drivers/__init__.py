"""
DR v2 destination drivers — LAN-only adapters.

Each driver implements :class:`DRDestinationDriver` and is registered in
:func:`build_driver` keyed by the v2 destination kind:

    local-disk          → local_disk.LocalDiskDriver
    nfs                 → nfs.NFSDriver
    smb                 → smb.SMBDriver
    sftp                → sftp.SFTPDriver
    minio-s3-onprem     → minio_s3.MinIOS3Driver  (AWS public hosts BANNED)
    tape-lto            → tape_lto.TapeLTODriver  (stubbed — TODO)
    usb-removable       → usb_removable.USBRemovableDriver (best-effort)

The cloud destinations from the legacy ``destinations.py`` (real AWS S3,
Azure Blob, GCS) are intentionally NOT exposed here.
"""
from __future__ import annotations

from typing import Any, Dict

from .base import DRDestinationDriver, DriverHealth, DriverWriteResult
from .local_disk import LocalDiskDriver
from .nfs import NFSDriver
from .smb import SMBDriver
from .sftp import SFTPDriver
from .minio_s3 import MinIOS3Driver
from .tape_lto import TapeLTODriver
from .usb_removable import USBRemovableDriver


_REGISTRY = {
    "local-disk": LocalDiskDriver,
    "nfs": NFSDriver,
    "smb": SMBDriver,
    "sftp": SFTPDriver,
    "minio-s3-onprem": MinIOS3Driver,
    "tape-lto": TapeLTODriver,
    "usb-removable": USBRemovableDriver,
}


def build_driver(kind: str, config: Dict[str, Any]) -> DRDestinationDriver:
    """Construct a driver from a v2 destination ``kind`` + config blob."""
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"unknown DR v2 destination kind: {kind!r}")
    return cls(**(config or {}))


def list_kinds() -> Dict[str, Dict[str, Any]]:
    """Return the kinds matrix shown in the admin UI capability picker."""
    return {
        "local-disk": {"available": True, "stub": False},
        "nfs": {"available": True, "stub": False},
        "smb": {"available": True, "stub": False},
        "sftp": {"available": True, "stub": False},
        "minio-s3-onprem": {"available": True, "stub": False,
                             "aws_blocked": True},
        "tape-lto": {"available": True, "stub": True},
        "usb-removable": {"available": True, "stub": False},
    }


__all__ = [
    "DRDestinationDriver",
    "DriverHealth",
    "DriverWriteResult",
    "build_driver",
    "list_kinds",
]
