"""Cluster manager — top-level read facade.

Combines membership + partition + identity + capabilities into one
``snapshot()`` for ops dashboards. Doesn't own a background loop;
the per-concern managers do.
"""

from __future__ import annotations

from typing import Optional

from app.distributed_system import (
    failure_detector, partition_detector, replication_manager,
)
from app.distributed_system.cluster_membership import get_cluster_membership
from app.distributed_system.consensus_manager import get_consensus_manager
from app.distributed_system.heartbeat_manager import get_heartbeat_manager
from app.distributed_system.node_capabilities import detect_local
from app.distributed_system.node_identity import identity_snapshot
from app.distributed_system.node_lifecycle import get_lifecycle


class ClusterManager:
    _singleton: "ClusterManager | None" = None

    @classmethod
    def instance(cls) -> "ClusterManager":
        if cls._singleton is None:
            cls._singleton = ClusterManager()
        return cls._singleton

    def members(self) -> set[str]:
        return get_cluster_membership().members()

    def member_count(self) -> int:
        return len(self.members())

    def is_majority(self) -> bool:
        return partition_detector.is_majority()

    def is_read_only(self) -> bool:
        return partition_detector.is_read_only()

    def lifecycle_state(self) -> str:
        return get_lifecycle().state().value

    def snapshot(self) -> dict:
        return {
            "identity":       identity_snapshot(),
            "capabilities":   detect_local().to_dict(),
            "lifecycle":      get_lifecycle().snapshot(),
            "members":        sorted(self.members()),
            "member_count":   self.member_count(),
            "is_majority":    self.is_majority(),
            "is_read_only":   self.is_read_only(),
            "partition":      partition_detector.snapshot(),
            "failure_detector": failure_detector.snapshot(),
            "replication":    replication_manager.stats(),
            "consensus":      get_consensus_manager().stats(),
        }


def get_cluster_manager() -> ClusterManager:
    return ClusterManager.instance()
