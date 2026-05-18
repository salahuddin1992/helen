"""Tests for saga_engine — forward execution, compensation on failure,
disk persistence + recovery, and TTL-based eviction."""
import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    """Each test gets its own data dir so saga_state.jsonl files don't
    leak between tests + cleanup is automatic."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("COMMCLIENT_DATA_DIR", tmp)
        # Reset the module-level paths since they're computed at import time.
        import importlib
        import app.services.saga_engine as sm
        importlib.reload(sm)
        yield Path(tmp)


# ── Tests ─────────────────────────────────────────────────────


async def test_basic_forward_execution(isolated_data_dir):
    from app.services.saga_engine import SagaEngine

    engine = SagaEngine()
    calls: list[str] = []

    async def step_one(state):
        calls.append("one")
        return {"x": 1}

    async def step_two(state):
        calls.append("two")
        assert state["x"] == 1
        return {"y": 2}

    engine.register("one", step_one)
    engine.register("two", step_two)

    s = await engine.run("test", [{"name": "one"}, {"name": "two"}])
    assert s.status.value == "completed"
    assert calls == ["one", "two"]
    assert s.state == {"x": 1, "y": 2}


async def test_compensation_on_failure(isolated_data_dir):
    from app.services.saga_engine import SagaEngine

    engine = SagaEngine()
    forward = []
    compensated = []

    async def step_a(state):
        forward.append("a")
        return None

    async def step_b(state):
        forward.append("b")
        raise RuntimeError("boom")

    async def comp_a(state):
        compensated.append("a")

    engine.register("a", step_a, compensate=comp_a)
    engine.register("b", step_b)

    s = await engine.run("with-compensation",
                         [{"name": "a"}, {"name": "b"}])
    assert s.status.value == "compensated"
    assert forward == ["a", "b"]
    # Step a was completed, so its compensation runs.
    assert compensated == ["a"]


async def test_no_handler_compensates_back(isolated_data_dir):
    from app.services.saga_engine import SagaEngine

    engine = SagaEngine()
    compensated = []

    async def step_a(state):
        return None

    async def comp_a(state):
        compensated.append("a")

    engine.register("a", step_a, compensate=comp_a)
    # Note: "missing" never registered.

    s = await engine.run("missing-handler",
                         [{"name": "a"}, {"name": "missing"}])
    assert s.status.value == "compensated"
    assert s.steps[1].error == "no_forward_handler"
    assert compensated == ["a"]


async def test_persistence_and_recovery(isolated_data_dir):
    """A saga marked RUNNING gets persisted; load_from_disk in a fresh
    engine instance re-hydrates it for inspection."""
    from app.services.saga_engine import SagaEngine, SagaStatus

    engine1 = SagaEngine()

    async def slow_step(state):
        # Simulate a never-completing step by sleeping.
        # We don't actually wait — we manually flip the saga to RUNNING
        # and persist, then act as if the process died.
        return None

    engine1.register("slow", slow_step)

    # Build a saga manually so we can persist it as RUNNING without
    # actually completing it.
    from app.services.saga_engine import Saga, SagaStep
    s = Saga(
        name="recovery-test",
        steps=[SagaStep(name="slow")],
        state={},
        status=SagaStatus.RUNNING,
    )
    engine1._sagas[s.saga_id] = s
    engine1._persist()

    # Load into a fresh engine — must recover the in-flight saga.
    engine2 = SagaEngine()
    loaded = engine2.load_from_disk()
    assert loaded == 1
    recovered = engine2.get(s.saga_id)
    assert recovered is not None
    assert recovered.name == "recovery-test"
    assert recovered.status == SagaStatus.RUNNING


async def test_resume_pending_completes_saga(isolated_data_dir):
    """A RUNNING saga whose steps had all completed (process died
    between last step and final flip) gets flipped to COMPLETED on
    resume."""
    from app.services.saga_engine import (
        Saga, SagaEngine, SagaStatus, SagaStep,
    )
    engine = SagaEngine()

    async def noop(state): return None
    engine.register("done", noop)

    s = Saga(
        name="all-completed-but-running",
        steps=[SagaStep(name="done", completed=True)],
        state={},
        status=SagaStatus.RUNNING,
    )
    engine._sagas[s.saga_id] = s

    resumed = await engine.resume_pending()
    assert resumed == 1
    assert engine.get(s.saga_id).status == SagaStatus.COMPLETED


async def test_evict_finished_drops_old_sagas(isolated_data_dir):
    """Completed sagas older than retention window get reclaimed."""
    import time as _t
    from app.services.saga_engine import (
        Saga, SagaEngine, SagaStatus,
    )
    engine = SagaEngine()
    engine.FINISHED_RETENTION_SEC = 0.05  # 50ms — tight for tests

    s = Saga(name="finished", steps=[], status=SagaStatus.COMPLETED,
             finished_at=_t.time() - 1.0)
    engine._sagas[s.saga_id] = s

    evicted = engine.evict_finished()
    assert evicted == 1
    assert engine.get(s.saga_id) is None


async def test_evict_finished_keeps_running(isolated_data_dir):
    """Running sagas are never evicted regardless of age."""
    from app.services.saga_engine import (
        Saga, SagaEngine, SagaStatus,
    )
    engine = SagaEngine()
    engine.FINISHED_RETENTION_SEC = 0.0

    s = Saga(name="alive", steps=[], status=SagaStatus.RUNNING,
             created_at=0.0)
    engine._sagas[s.saga_id] = s

    evicted = engine.evict_finished()
    assert evicted == 0
    assert engine.get(s.saga_id) is not None
