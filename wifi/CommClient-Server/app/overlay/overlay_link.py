"""OverlayLink — a logical edge between two OverlayNodes.

The link is *application-defined* — it might map to a single TCP
connection, a logical pub/sub subscription, or a routing table
entry. The overlay layer doesn't open any sockets itself; it just
represents the intent.

Edges are directed; bidirectional connectivity = two opposing
links.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict


@dataclass
class OverlayLink:
    overlay_name: str
    src_id:       str
    dst_id:       str
    weight:       float = 1.0          # higher = preferred path
    bidirectional_hint: bool = False   # informational only
    last_seen:    float = field(default_factory=time.time)
    metadata:     dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        """Edge identity: (overlay_name, src_id, dst_id)."""
        return (self.overlay_name, self.src_id, self.dst_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OverlayLink) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def is_fresh(self, max_age_sec: float = 120.0) -> bool:
        return (time.time() - self.last_seen) <= max_age_sec

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "OverlayLink":
        return cls(
            overlay_name=str(data["overlay_name"]),
            src_id=str(data["src_id"]),
            dst_id=str(data["dst_id"]),
            weight=float(data.get("weight") or 1.0),
            bidirectional_hint=bool(data.get("bidirectional_hint") or False),
            last_seen=float(data.get("last_seen") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )
