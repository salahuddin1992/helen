"""OverlayNode — a logical participant in an overlay.

An overlay node is *not* the same as a physical peer. Multiple
overlay nodes can map to the same underlying peer (one node per
overlay), and a single overlay name can span peers from different
clusters.

The OverlayNode carries:
  * overlay_name  — which logical overlay this membership belongs to
  * node_id       — overlay-local identifier (often = underlying peer_id)
  * peer_id       — the physical peer this overlay node lives on
  * tags          — application-defined labels (topic, role, etc.)
  * metadata      — free-form payload (sequence numbers, capability)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class OverlayNode:
    overlay_name: str
    node_id:      str
    peer_id:      str = ""
    tags:         set[str] = field(default_factory=set)
    metadata:     dict[str, Any] = field(default_factory=dict)
    last_seen:    float = field(default_factory=time.time)

    @property
    def key(self) -> tuple[str, str]:
        """Identity within the overlay registry: (overlay_name, node_id)."""
        return (self.overlay_name, self.node_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OverlayNode) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def is_fresh(self, max_age_sec: float = 60.0) -> bool:
        return (time.time() - self.last_seen) <= max_age_sec

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tags"] = sorted(self.tags)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "OverlayNode":
        return cls(
            overlay_name=str(data["overlay_name"]),
            node_id=str(data["node_id"]),
            peer_id=str(data.get("peer_id") or ""),
            tags=set(data.get("tags") or []),
            metadata=dict(data.get("metadata") or {}),
            last_seen=float(data.get("last_seen") or time.time()),
        )
