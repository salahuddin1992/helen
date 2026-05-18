"""Custom exception hierarchy for the distributed-system package.

A single base (``DistributedSystemError``) lets callers catch
everything from this package with one ``except`` clause; specific
subclasses let callers act on individual failure modes (e.g. don't
retry on QuorumLostError).
"""

from __future__ import annotations


class DistributedSystemError(Exception):
    """Base class for every distributed_system exception."""


class NodeNotFoundError(DistributedSystemError):
    """Raised when a referenced node_id has no entry in the registry."""


class ClusterMembershipError(DistributedSystemError):
    """JOIN/LEAVE/EVICT decision failed."""


class LeaderElectionError(DistributedSystemError):
    """Raised when the lock cannot be acquired or the leader lease
    cannot be renewed."""


class ConsensusError(DistributedSystemError):
    """Generic consensus-write failure; specific subclasses follow."""


class QuorumLostError(ConsensusError):
    """Quorum write completed but with fewer acks than required."""


class ReplicationError(DistributedSystemError):
    """Replication push could not establish the required replica count."""


class ConsistencyError(DistributedSystemError):
    """Caller-requested consistency level cannot be satisfied."""


class StateSyncError(DistributedSystemError):
    """State-sync exchange with a peer failed."""


class ShardError(DistributedSystemError):
    """Shard owner lookup or migration failed."""


class TaskDistributionError(DistributedSystemError):
    """A distributed task could not be assigned to any node."""


class HeartbeatError(DistributedSystemError):
    """Heartbeat could not be sent or recorded."""


class FailureDetectorError(DistributedSystemError):
    """The failure detector raised on a probe."""


class RecoveryError(DistributedSystemError):
    """Recovery orchestration failed."""


class PartitionDetectedError(DistributedSystemError):
    """Raised when the partition detector observes minority status
    while the caller required majority."""


class GossipError(DistributedSystemError):
    """Gossip exchange failed."""
