"""
Replicated audit trail — hash-chained, tamper-evident, distributed.

Every operator action that touches sync_policy, peer approval, trust
DB, or capacity overrides is consequential. Local audit logs already
exist (``peer_approval_audit.py``) but they live on a single box —
if that box is compromised or dies, the trail is gone or rewritable.

This module replicates the audit stream across the cluster as a
hash-linked chain:

    entry₀ = sha256("genesis" + payload₀)
    entry₁ = sha256(entry₀ + payload₁)
    entry₂ = sha256(entry₁ + payload₂)

Any peer can verify the chain by re-hashing — a tampered intermediate
entry breaks every following hash. Combined with replication across
≥ 3 peers, an attacker would need to corrupt the same entries on
every replica simultaneously, which is the point.

Storage
-------
Local: ``data/audit_chain.jsonl`` (append-only).
Each line:
    {
      "seq":       integer monotonic,
      "timestamp": float (cluster_time),
      "event":     str ("peer_approved", "trust_reset", ...),
      "actor":     str (user_id or system),
      "payload":   dict (event-specific),
      "prev_hash": str (sha256 of the previous entry's full record),
      "this_hash": str (sha256 of this entry sans this_hash field),
      "origin":    str (server_id of writer),
    }

Replication
-----------
On write, the entry is broadcast to ``REPLICATION_FANOUT`` peers via
``POST /api/cluster/audit/replicate``. Receivers append to their
local chain only if the chain check passes (prev_hash matches their
last_hash OR they have a gap and request backfill).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_CHAIN_FILE = _DATA_DIR / "audit_chain.jsonl"
_GENESIS = "helen-audit-genesis-v1"

REPLICATION_FANOUT  = 3
REPLICATION_TIMEOUT = 3.0


# ── Hash helpers ────────────────────────────────────────────────


def _entry_hash(entry: dict) -> str:
    """sha256 over a canonical JSON of every field except ``this_hash``."""
    canonical = {k: v for k, v in entry.items() if k != "this_hash"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode()
    ).hexdigest()


# ── Replicator ──────────────────────────────────────────────────


class AuditReplicator:
    _singleton: "AuditReplicator | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_hash: str = self._init_genesis()
        self._seq: int = self._init_seq()

    @classmethod
    def instance(cls) -> "AuditReplicator":
        if cls._singleton is None:
            cls._singleton = AuditReplicator()
        return cls._singleton

    def _init_genesis(self) -> str:
        try:
            if _CHAIN_FILE.is_file():
                last_line = ""
                with _CHAIN_FILE.open("r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            last_line = line
                if last_line:
                    last = json.loads(last_line)
                    return last.get("this_hash") or _GENESIS
        except Exception as e:
            logger.warning("audit_chain_load_failed", error=str(e))
        return _GENESIS

    def _init_seq(self) -> int:
        try:
            if _CHAIN_FILE.is_file():
                last_line = ""
                with _CHAIN_FILE.open("r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            last_line = line
                if last_line:
                    last = json.loads(last_line)
                    return int(last.get("seq", 0))
        except Exception:
            pass
        return 0

    # ── Public write API ────────────────────────────────────

    def append_local(
        self,
        event: str,
        actor: str = "system",
        payload: Optional[dict] = None,
    ) -> dict:
        """Add a new entry to the local chain. Replication to peers
        happens via the start_audit_replication_loop() background
        task — append_local just hands the entry to the chain.
        """
        from app.services.discovery_service import get_server_id
        from app.services.cluster_time import get_cluster_time

        with self._lock:
            self._seq += 1
            entry = {
                "seq":       self._seq,
                "timestamp": get_cluster_time().now(),
                "event":     event,
                "actor":     actor,
                "payload":   payload or {},
                "prev_hash": self._last_hash,
                "origin":    get_server_id() or "anon",
            }
            entry["this_hash"] = _entry_hash(entry)
            self._last_hash = entry["this_hash"]

            try:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                with _CHAIN_FILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e:
                logger.error("audit_chain_write_failed", error=str(e))

            # Schedule replication best-effort.
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(_replicate(entry))
            except RuntimeError:
                pass

            return entry

    def absorb_remote(self, entry: dict) -> tuple[bool, str]:
        """Apply an entry from a peer if it links into our chain.

        Returns (accepted, reason). The chain check requires:
          * entry.this_hash recomputes correctly,
          * entry.prev_hash matches our current last_hash.
        Otherwise we return (False, "fork") and let the gap-fill
        path catch up later.
        """
        try:
            recomputed = _entry_hash(entry)
        except Exception as e:
            return False, f"bad_payload:{e}"
        if recomputed != entry.get("this_hash"):
            return False, "hash_mismatch"

        with self._lock:
            if entry.get("prev_hash") != self._last_hash:
                # Fork or gap. Caller may request backfill.
                return False, "prev_hash_mismatch"

            try:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                with _CHAIN_FILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
                self._last_hash = entry["this_hash"]
                self._seq = max(self._seq, int(entry.get("seq", 0)))
            except Exception as e:
                return False, f"write_failed:{e}"

        return True, "ok"

    def head(self) -> dict:
        with self._lock:
            return {
                "seq":       self._seq,
                "last_hash": self._last_hash,
            }

    def verify_chain(self, max_entries: int = 10_000) -> dict:
        """Re-hash every entry; return verification result."""
        if not _CHAIN_FILE.is_file():
            return {"ok": True, "entries": 0, "broken_at": None}
        prev = _GENESIS
        count = 0
        with _CHAIN_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                if count >= max_entries:
                    break
                try:
                    e = json.loads(line)
                except Exception:
                    return {"ok": False, "entries": count, "broken_at": count}
                if e.get("prev_hash") != prev:
                    return {"ok": False, "entries": count, "broken_at": e.get("seq")}
                if _entry_hash(e) != e.get("this_hash"):
                    return {"ok": False, "entries": count, "broken_at": e.get("seq")}
                prev = e.get("this_hash") or prev
                count += 1
        return {"ok": True, "entries": count, "broken_at": None}


def get_audit_replicator() -> AuditReplicator:
    return AuditReplicator.instance()


# ── Replication network ─────────────────────────────────────────


async def _replicate(entry: dict) -> None:
    try:
        import httpx
        from app.core.federation_auth import sign_request
        from app.services.node_registry import get_registry
    except ImportError:
        return

    reg = get_registry()
    peers = [n for n in reg.nodes(include_dead=False) if not n.self_node]
    if not peers:
        return

    import random
    targets = random.sample(peers, k=min(REPLICATION_FANOUT, len(peers)))
    body = json.dumps({"entry": entry}).encode()
    path = "/api/cluster/audit/replicate"
    headers = sign_request("POST", path, body)
    headers["Content-Type"] = "application/json"

    async def _send(peer):
        try:
            async with httpx.AsyncClient(timeout=REPLICATION_TIMEOUT) as c:
                await c.post(
                    f"http://{peer.host}:{peer.port}{path}",
                    content=body, headers=headers,
                )
        except Exception as e:
            logger.debug("audit_replicate_failed",
                         peer=peer.node_id[:24], error=str(e)[:80])

    await asyncio.gather(*(_send(p) for p in targets), return_exceptions=True)
