"""
LTO tape driver — stubbed best-effort.

TODO(real-implementation):
    * mt-st / mtx for cartridge manipulation
    * LTFS for filesystem semantics on tape
    * pyltfs / ltfsee bindings
    * tape barcode tracking, robotic library control

For now this driver writes to a local "tape staging" folder structured
as if it were on tape, and reports as a stub. Production deployments
should replace this driver before relying on it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.core.logging import get_logger

from .base import DriverHealth
from .local_disk import LocalDiskDriver


logger = get_logger(__name__)


class TapeLTODriver(LocalDiskDriver):
    kind = "tape-lto"

    def __init__(
        self,
        device: str = "/dev/nst0",
        staging_root: str = "data/dr_v2/tape_stage",
        barcode: str | None = None,
        library: str | None = None,
        slot: int | None = None,
        **_: Any,
    ) -> None:
        # local-disk semantics for the staging area until the real driver lands
        super().__init__(root=staging_root)
        self.device = device
        self.barcode = barcode
        self.library = library
        self.slot = slot
        logger.warning(
            "dr_tape_lto_stubbed",
            device=device, staging=staging_root,
            note="LTO driver is a best-effort stub — replace before production use",
        )

    async def test(self) -> DriverHealth:
        h = await super().test()
        h.kind = self.kind
        h.details.update({
            "device": self.device,
            "barcode": self.barcode,
            "library": self.library,
            "slot": self.slot,
            "stub": True,
            "warning": "LTO driver is currently a staging-folder stub. "
                       "Cartridge manipulation, LTFS, and barcode tracking "
                       "are NOT implemented.",
        })
        return h
