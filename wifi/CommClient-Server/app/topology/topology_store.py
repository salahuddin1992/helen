"""Topology persistence — JSON snapshots of the graph.

The graph is in-memory primary, but we want to:

  * Restore last-known topology on restart so the new process can
    start routing immediately, before the discovery loops have time
    to repopulate from broadcasts.
  * Provide a stable artefact for ops dashboards / external audit.

Format: a single ``data/topology.json`` with the same schema
``TopologyGraph.to_dict`` produces. Atomic via temp-file + rename.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_FILE = _DATA_DIR / "topology.json"


class TopologyStore:
    """Thin persistence layer — singleton wrapping the JSON file."""

    _singleton: "TopologyStore | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_save_at: float = 0.0
        self._last_save_bytes: int = 0

    @classmethod
    def instance(cls) -> "TopologyStore":
        if cls._singleton is None:
            cls._singleton = TopologyStore()
        return cls._singleton

    # ── Save ─────────────────────────────────────────────────

    def save(self, graph_dict: dict) -> bool:
        """Atomically write the graph to disk. Returns True on success."""
        with self._lock:
            try:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                payload = {
                    "version":    1,
                    "saved_at":   time.time(),
                    **graph_dict,
                }
                tmp = _FILE.with_suffix(".tmp")
                content = json.dumps(payload, sort_keys=True, indent=2)
                tmp.write_text(content, encoding="utf-8")
                shutil.move(str(tmp), str(_FILE))
                self._last_save_at = time.time()
                self._last_save_bytes = len(content.encode())
                return True
            except Exception as e:
                logger.warning("topology_store_save_failed", error=str(e))
                return False

    # ── Load ─────────────────────────────────────────────────

    def load(self) -> Optional[dict]:
        with self._lock:
            try:
                if not _FILE.is_file():
                    return None
                data = json.loads(_FILE.read_text(encoding="utf-8"))
                if data.get("version") != 1:
                    logger.warning(
                        "topology_store_version_mismatch",
                        expected=1, got=data.get("version"),
                    )
                return data
            except Exception as e:
                logger.warning("topology_store_load_failed", error=str(e))
                return None

    # ── Diagnostics ──────────────────────────────────────────

    def info(self) -> dict:
        return {
            "path":             str(_FILE),
            "exists":           _FILE.is_file(),
            "last_save_at":     self._last_save_at,
            "last_save_bytes":  self._last_save_bytes,
            "size_on_disk":     _FILE.stat().st_size if _FILE.is_file() else 0,
        }


def get_topology_store() -> TopologyStore:
    return TopologyStore.instance()
