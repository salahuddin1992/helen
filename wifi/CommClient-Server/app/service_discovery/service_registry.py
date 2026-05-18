"""Service registry — in-memory + JSON-persisted store.

Indexed by:
  * service_id (primary key)
  * service_type (for fast type-filtered lookup)
  * (region, zone) (for locality-filtered lookup)

All mutations go through one RLock so the registry is safe to call
from sync + async paths. Persistence to ``data/service_registry.json``
is best-effort and never blocks the writer.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

from app.core.logging import get_logger
from app.service_discovery.discovery_config import get_config
from app.service_discovery.discovery_events import emit
from app.service_discovery.discovery_exceptions import (
    ServiceNotFoundError, ServiceRegistrationError,
)
from app.service_discovery.service_record import (
    ServiceRecord, ServiceStatus, ServiceType,
)
from app.service_discovery.service_signing import verify_record

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_PERSIST_FILE = _DATA_DIR / "service_registry.json"


class ServiceRegistry:
    _singleton: "ServiceRegistry | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, ServiceRecord] = {}
        # Secondary indexes — rebuild on every mutation for simplicity.
        self._by_type: dict[ServiceType, set[str]] = defaultdict(set)
        self._by_locality: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._dirty = False

    @classmethod
    def instance(cls) -> "ServiceRegistry":
        if cls._singleton is None:
            cls._singleton = ServiceRegistry()
            cls._singleton._restore()
        return cls._singleton

    # ── CRUD ──────────────────────────────────────────────

    def register(self, record: ServiceRecord, *,
                 verify_signature: bool = True) -> ServiceRecord:
        if verify_signature:
            ok, reason = verify_record(record)
            if not ok:
                emit("service.rejected", {
                    "reason":    reason,
                    "service_id": record.service_id[:24],
                    "type":      record.service_type.value,
                })
                raise ServiceRegistrationError(f"signature: {reason}")

        if not record.host or record.port <= 0:
            raise ServiceRegistrationError("host/port required")

        with self._lock:
            existing = self._records.get(record.service_id)
            if existing is None:
                self._records[record.service_id] = record
                self._by_type[record.service_type].add(record.service_id)
                self._by_locality[
                    (record.region, record.zone)
                ].add(record.service_id)
                emit("service.registered", {
                    "service_id":  record.service_id[:24],
                    "type":        record.service_type.value,
                    "host":        f"{record.host}:{record.port}",
                    "region":      record.region,
                })
                self._dirty = True
                return record

            # Update path — preserve registered_at, refresh the rest.
            existing.host = record.host
            existing.port = record.port
            existing.protocol = record.protocol
            existing.public_url = record.public_url
            existing.cluster_id = record.cluster_id
            existing.region = record.region
            existing.zone = record.zone
            existing.ttl_sec = record.ttl_sec
            existing.max_capacity = record.max_capacity
            existing.current_load = record.current_load
            existing.capacity_pct = record.capacity_pct
            existing.advertised_latency_ms = record.advertised_latency_ms
            existing.capabilities.update(record.capabilities or {})
            existing.tags |= record.tags
            existing.signature = record.signature
            existing.signed_at = record.signed_at
            existing.pubkey_fingerprint = record.pubkey_fingerprint
            existing.beat()
            self._dirty = True
            return existing

    def heartbeat(self, service_id: str, *,
                  current_load: int | None = None,
                  status: ServiceStatus | None = None) -> ServiceRecord:
        with self._lock:
            record = self._records.get(service_id)
            if record is None:
                raise ServiceNotFoundError(service_id)
            record.beat(current_load=current_load, status=status)
            self._dirty = True
        emit("service.heartbeat", {
            "service_id": service_id[:24],
            "load":       current_load,
            "status":     (status or record.status).value
                          if hasattr(status or record.status, "value")
                          else None,
        })
        return record

    def deregister(self, service_id: str) -> bool:
        with self._lock:
            record = self._records.pop(service_id, None)
            if record is None:
                return False
            self._by_type[record.service_type].discard(service_id)
            self._by_locality[
                (record.region, record.zone)
            ].discard(service_id)
            self._dirty = True
        emit("service.deregistered", {"service_id": service_id[:24]})
        return True

    def get(self, service_id: str) -> Optional[ServiceRecord]:
        with self._lock:
            return self._records.get(service_id)

    def all(self) -> list[ServiceRecord]:
        with self._lock:
            return list(self._records.values())

    # ── Filtered queries ─────────────────────────────────

    def by_type(self, service_type: ServiceType) -> list[ServiceRecord]:
        with self._lock:
            return [
                self._records[sid]
                for sid in self._by_type.get(service_type, set())
                if sid in self._records
            ]

    def by_region(self, region: str) -> list[ServiceRecord]:
        with self._lock:
            return [
                r for r in self._records.values()
                if r.region == region
            ]

    def healthy(self,
                grace_sec: float | None = None) -> list[ServiceRecord]:
        cfg = get_config()
        g = grace_sec if grace_sec is not None else cfg.heartbeat_grace_sec
        with self._lock:
            return [
                r for r in self._records.values()
                if r.is_alive(grace_sec=g)
                and r.status == ServiceStatus.HEALTHY
            ]

    def stale(self,
              grace_sec: float | None = None) -> list[ServiceRecord]:
        cfg = get_config()
        g = grace_sec if grace_sec is not None else cfg.heartbeat_grace_sec
        with self._lock:
            return [
                r for r in self._records.values()
                if r.is_dead(grace_sec=g)
            ]

    # ── Persistence ──────────────────────────────────────

    def persist_if_dirty(self) -> bool:
        cfg = get_config()
        if not cfg.persist_to_disk:
            return False
        with self._lock:
            if not self._dirty:
                return False
            payload = {
                "records": [r.to_dict() for r in self._records.values()],
                "saved_at": time.time(),
            }
            self._dirty = False
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _PERSIST_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, sort_keys=True),
                           encoding="utf-8")
            shutil.move(str(tmp), str(_PERSIST_FILE))
            return True
        except Exception as e:
            logger.warning("sd_registry_persist_failed", error=str(e))
            return False

    def _restore(self) -> int:
        if not _PERSIST_FILE.is_file():
            return 0
        try:
            data = json.loads(_PERSIST_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("sd_registry_restore_failed", error=str(e))
            return 0
        n = 0
        for raw in data.get("records") or []:
            try:
                r = ServiceRecord.from_dict(raw)
                with self._lock:
                    self._records[r.service_id] = r
                    self._by_type[r.service_type].add(r.service_id)
                    self._by_locality[(r.region, r.zone)].add(r.service_id)
                n += 1
            except Exception:
                continue
        return n

    # ── Diagnostics ──────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            by_type_count = {
                k.value: len(v)
                for k, v in self._by_type.items()
                if v
            }
            return {
                "total":        len(self._records),
                "by_type":      by_type_count,
                "regions":      sorted({r.region for r in self._records.values()}),
                "healthy":      len(self.healthy()),
                "stale":        len(self.stale()),
            }


def get_registry() -> ServiceRegistry:
    return ServiceRegistry.instance()
