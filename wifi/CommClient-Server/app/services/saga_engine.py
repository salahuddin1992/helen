"""Saga engine — multi-step distributed transactions with compensations.

A saga is a sequence of forward steps. If step N fails, the engine
runs the *compensation* of every previously-completed step in
reverse order, leaving the system in a consistent state.

Typical use cases:

  * "Onboard new server"  (provision keys → register peer → broadcast
                           announce → mark active). If announce fails,
                           we de-register and revoke keys.
  * "Move call to SFU"    (allocate SFU slot → migrate participants →
                           tear down mesh peers). If migration fails,
                           free the SFU slot.

State is persisted to ``data/saga_state.jsonl`` so a process restart
can resume in-flight sagas. Each saga has a UUID; its execution log
records (step_index, kind, ok, error, ts).

Forward + compensation steps are async callables registered by name
into the global registry. The saga itself is a list of step names +
their bound argument dicts.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


_DATA_DIR = Path(os.environ.get("COMMCLIENT_DATA_DIR",
                 str(Path(__file__).resolve().parents[2] / "data")))
_STATE_FILE = _DATA_DIR / "saga_state.jsonl"


class SagaStatus(str, Enum):
    PENDING       = "pending"
    RUNNING       = "running"
    COMPENSATING  = "compensating"
    COMPLETED     = "completed"
    COMPENSATED   = "compensated"
    FAILED        = "failed"


# Step signature: (state_dict) → awaitable[dict-or-None]. The returned
# dict is merged into the saga state for later steps.
StepFn = Callable[[dict], Awaitable[Optional[dict]]]


@dataclass
class SagaStep:
    name:           str
    args:           dict = field(default_factory=dict)
    completed:      bool = False
    compensated:    bool = False
    error:          str = ""
    started_at:     float = 0.0
    finished_at:    float = 0.0


@dataclass
class Saga:
    saga_id:    str = field(default_factory=lambda: uuid.uuid4().hex)
    name:       str = ""
    steps:      list[SagaStep] = field(default_factory=list)
    state:      dict = field(default_factory=dict)
    status:     SagaStatus = SagaStatus.PENDING
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "saga_id":     self.saga_id,
            "name":        self.name,
            "status":      self.status.value,
            "created_at":  self.created_at,
            "finished_at": self.finished_at,
            "steps":       [asdict(s) for s in self.steps],
            "state":       dict(self.state),
        }


class SagaEngine:
    _singleton: "SagaEngine | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sagas: dict[str, Saga] = {}
        self._forward: dict[str, StepFn] = {}
        self._compensate: dict[str, StepFn] = {}

    @classmethod
    def instance(cls) -> "SagaEngine":
        if cls._singleton is None:
            cls._singleton = SagaEngine()
        return cls._singleton

    # ── Step registration ────────────────────────────────

    def register(self, name: str, forward: StepFn,
                 compensate: StepFn | None = None) -> None:
        with self._lock:
            self._forward[name] = forward
            if compensate is not None:
                self._compensate[name] = compensate

    # ── Saga lifecycle ───────────────────────────────────

    async def run(self, name: str, steps: list[dict],
                  *, initial_state: dict | None = None) -> Saga:
        """Execute a saga. ``steps`` is a list of {name, args} dicts."""
        s = Saga(
            name=name,
            steps=[SagaStep(name=str(s["name"]),
                            args=dict(s.get("args") or {}))
                   for s in steps],
            state=dict(initial_state or {}),
            status=SagaStatus.RUNNING,
        )
        with self._lock:
            self._sagas[s.saga_id] = s
        self._persist()

        for i, step in enumerate(s.steps):
            with self._lock:
                fn = self._forward.get(step.name)
            if fn is None:
                step.error = "no_forward_handler"
                await self._compensate_back(s, until=i - 1)
                s.status = SagaStatus.COMPENSATED
                s.finished_at = time.time()
                self._persist()
                return s
            step.started_at = time.time()
            try:
                merged_state = {**s.state, **step.args}
                result = await fn(merged_state)
                if result:
                    s.state.update(result)
                step.completed = True
                step.finished_at = time.time()
            except Exception as e:
                step.error = str(e)[:200]
                step.finished_at = time.time()
                logger.warning("saga_step_failed",
                               saga_id=s.saga_id,
                               step=step.name, error=step.error)
                await self._compensate_back(s, until=i - 1)
                s.status = SagaStatus.COMPENSATED
                s.finished_at = time.time()
                self._persist()
                return s

        s.status = SagaStatus.COMPLETED
        s.finished_at = time.time()
        self._persist()
        return s

    async def _compensate_back(self, s: Saga, until: int) -> None:
        s.status = SagaStatus.COMPENSATING
        for i in range(until, -1, -1):
            step = s.steps[i]
            if not step.completed:
                continue
            fn = self._compensate.get(step.name)
            if fn is None:
                step.error = (step.error or "") + "; no_compensation"
                continue
            try:
                merged_state = {**s.state, **step.args}
                await fn(merged_state)
                step.compensated = True
            except Exception as e:
                step.error = (step.error or "") + f"; compensate_failed:{e}"

    # ── Persistence ──────────────────────────────────────

    def _persist(self) -> None:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            with self._lock:
                rows = [s.to_dict() for s in self._sagas.values()
                        if s.status in (SagaStatus.RUNNING,
                                        SagaStatus.COMPENSATING)]
            tmp = _STATE_FILE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            tmp.replace(_STATE_FILE)
        except Exception as e:
            logger.debug("saga_persist_failed", error=str(e)[:80])

    def load_from_disk(self) -> int:
        """
        Restore in-flight sagas from ``data/saga_state.jsonl`` after a
        process restart. Each restored saga keeps its previous status
        (RUNNING / COMPENSATING) so the operator can see what the
        previous instance was doing; resuming execution requires a
        separate explicit call to ``resume_pending()`` so we never
        replay forward steps automatically (could double-charge a
        non-idempotent operation).

        Returns the count of sagas loaded. Safe to call multiple times
        — already-loaded saga_ids are not duplicated.
        """
        if not _STATE_FILE.exists():
            return 0
        loaded = 0
        try:
            with _STATE_FILE.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    saga_id = d.get("saga_id")
                    if not saga_id:
                        continue
                    with self._lock:
                        if saga_id in self._sagas:
                            continue
                        steps = [
                            SagaStep(
                                name=str(sd.get("name", "")),
                                args=dict(sd.get("args") or {}),
                                completed=bool(sd.get("completed", False)),
                                compensated=bool(sd.get("compensated", False)),
                                error=str(sd.get("error", "")),
                                started_at=float(sd.get("started_at", 0.0)),
                                finished_at=float(sd.get("finished_at", 0.0)),
                            )
                            for sd in (d.get("steps") or [])
                        ]
                        try:
                            status = SagaStatus(d.get("status", "running"))
                        except ValueError:
                            status = SagaStatus.RUNNING
                        s = Saga(
                            saga_id=saga_id,
                            name=str(d.get("name", "")),
                            steps=steps,
                            state=dict(d.get("state") or {}),
                            status=status,
                            created_at=float(d.get("created_at", time.time())),
                            finished_at=float(d.get("finished_at", 0.0)),
                        )
                        self._sagas[saga_id] = s
                    loaded += 1
        except Exception as e:
            logger.warning("saga_load_failed", error=str(e))
        if loaded:
            logger.info("saga_recovered_from_disk", count=loaded)
        return loaded

    async def resume_pending(self) -> int:
        """
        Re-drive any RUNNING saga whose current step has not yet
        completed. Compensation paths are never auto-resumed —
        operator must inspect and trigger explicitly via the admin
        endpoint. Returns count resumed.
        """
        with self._lock:
            running = [s for s in self._sagas.values()
                       if s.status == SagaStatus.RUNNING]
        resumed = 0
        for s in running:
            # Find the first not-yet-completed step.
            idx = next((i for i, st in enumerate(s.steps) if not st.completed), None)
            if idx is None:
                # All steps already completed but status was not flipped
                # (process died between last step and final flip).
                with self._lock:
                    s.status = SagaStatus.COMPLETED
                    s.finished_at = time.time()
                self._persist()
                resumed += 1
                continue
            # Re-execute from the unfinished step. Idempotent forward
            # handlers will silently no-op; non-idempotent ones must
            # handle their own dedup logic.
            try:
                await self._execute_from(s, idx)
                resumed += 1
            except Exception as e:
                logger.warning("saga_resume_failed",
                               saga_id=s.saga_id, error=str(e))
        return resumed

    async def _execute_from(self, s: Saga, start: int) -> None:
        """Continue running a saga from the given step index."""
        for i in range(start, len(s.steps)):
            step = s.steps[i]
            with self._lock:
                fn = self._forward.get(step.name)
            if fn is None:
                step.error = "no_forward_handler"
                await self._compensate_back(s, until=i - 1)
                s.status = SagaStatus.COMPENSATED
                s.finished_at = time.time()
                self._persist()
                return
            step.started_at = time.time()
            try:
                merged_state = {**s.state, **step.args}
                result = await fn(merged_state)
                if result:
                    s.state.update(result)
                step.completed = True
                step.finished_at = time.time()
            except Exception as e:
                step.error = str(e)[:200]
                step.finished_at = time.time()
                await self._compensate_back(s, until=i - 1)
                s.status = SagaStatus.COMPENSATED
                s.finished_at = time.time()
                self._persist()
                return
        s.status = SagaStatus.COMPLETED
        s.finished_at = time.time()
        self._persist()

    # ── Diagnostics ──────────────────────────────────────

    def get(self, saga_id: str) -> Saga | None:
        with self._lock:
            return self._sagas.get(saga_id)

    def list(self, *, limit: int = 50) -> list[dict]:
        with self._lock:
            sagas = list(self._sagas.values())
        sagas.sort(key=lambda s: s.created_at, reverse=True)
        return [s.to_dict() for s in sagas[:limit]]

    def stats(self) -> dict:
        with self._lock:
            sagas = list(self._sagas.values())
        by_status: dict[str, int] = {}
        for s in sagas:
            by_status[s.status.value] = by_status.get(s.status.value, 0) + 1
        return {
            "total":         len(sagas),
            "by_status":     by_status,
            "registered_steps": sorted(self._forward.keys()),
        }

    # Retain finished sagas this long for `list()` diagnostics, then drop
    # them so the in-memory dict doesn't grow unbounded over the
    # lifetime of the server.
    FINISHED_RETENTION_SEC = 3600.0

    def evict_finished(self) -> int:
        """Drop COMPLETED / COMPENSATED / FAILED sagas older than the
        retention window. Running ones are always kept. Returns count
        evicted. Safe to call from a periodic task."""
        cutoff = time.time() - self.FINISHED_RETENTION_SEC
        terminal = {SagaStatus.COMPLETED, SagaStatus.COMPENSATED, SagaStatus.FAILED}
        with self._lock:
            stale = [
                sid for sid, s in self._sagas.items()
                if s.status in terminal
                and s.finished_at
                and s.finished_at < cutoff
            ]
            for sid in stale:
                self._sagas.pop(sid, None)
        return len(stale)


def get_saga_engine() -> SagaEngine:
    return SagaEngine.instance()
