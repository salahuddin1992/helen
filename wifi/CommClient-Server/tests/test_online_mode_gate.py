"""Tests for the online-mode master gate."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest


# ── Persistence ──────────────────────────────────────────────────


def test_default_state_is_off(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HELEN_ONLINE_MODE_DEFAULT", raising=False)
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    state = tmp_path / "online_mode.json"
    g = configure_online_mode_gate(state)
    g._load_state()
    assert g.enabled is False
    reset_online_mode_gate()


def test_default_state_respects_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HELEN_ONLINE_MODE_DEFAULT", "on")
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    state = tmp_path / "online_mode.json"
    g = configure_online_mode_gate(state)
    g._load_state()
    assert g.enabled is True
    reset_online_mode_gate()


@pytest.mark.asyncio
async def test_state_persists_across_instances(tmp_path: Path, monkeypatch):
    """Don't use ``asyncio.run`` here — it would close the session-scoped
    event loop the rest of the file's async tests rely on (see
    ``tests/conftest.py``)."""
    monkeypatch.delenv("HELEN_ONLINE_MODE_DEFAULT", raising=False)
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    state = tmp_path / "online_mode.json"
    g1 = configure_online_mode_gate(state)

    await g1.enable(actor="admin-1", reason="testing")

    assert state.exists()
    payload = json.loads(state.read_text())
    assert payload["enabled"] is True
    assert payload["last_actor"] == "admin-1"

    # Fresh instance, same path → loads "on".
    g2 = configure_online_mode_gate(state)
    g2._load_state()
    assert g2.enabled is True
    reset_online_mode_gate()


def test_corrupted_state_falls_back_off(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HELEN_ONLINE_MODE_DEFAULT", raising=False)
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    state = tmp_path / "online_mode.json"
    state.write_text("{not valid json")
    g = configure_online_mode_gate(state)
    g._load_state()
    assert g.enabled is False
    reset_online_mode_gate()


# ── Service registration / lifecycle ─────────────────────────────


@pytest.mark.asyncio
async def test_register_and_enable_starts_services(tmp_path: Path):
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    g = configure_online_mode_gate(tmp_path / "om.json")

    started: list[str] = []
    stopped: list[str] = []

    async def start_a(): started.append("a")
    async def stop_a(): stopped.append("a")

    g.register("a", start=start_a, stop=stop_a)
    await g.bootstrap()  # state is OFF by default → nothing started
    assert started == []

    await g.enable(actor="t", reason="r")
    assert started == ["a"]
    snap = g.status()
    assert snap["enabled"] is True
    assert snap["services"][0]["running"] is True

    await g.disable(actor="t", reason="r")
    assert stopped == ["a"]
    reset_online_mode_gate()


@pytest.mark.asyncio
async def test_enable_twice_is_idempotent(tmp_path: Path):
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    g = configure_online_mode_gate(tmp_path / "om.json")

    starts = 0

    async def start():
        nonlocal starts
        starts += 1

    async def stop():
        pass

    g.register("svc", start=start, stop=stop)
    await g.enable()
    await g.enable()
    await g.enable()
    assert starts == 1
    reset_online_mode_gate()


@pytest.mark.asyncio
async def test_failed_start_is_logged_but_does_not_break_others(tmp_path: Path):
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    g = configure_online_mode_gate(tmp_path / "om.json")

    async def good_start(): pass
    async def good_stop(): pass

    async def bad_start(): raise RuntimeError("boom")
    async def bad_stop(): pass

    g.register("bad", start=bad_start, stop=bad_stop)
    g.register("good", start=good_start, stop=good_stop)
    await g.enable()
    snap = g.status()
    services = {s["name"]: s for s in snap["services"]}
    assert services["bad"]["running"] is False
    assert services["bad"]["last_error"] == "boom"
    assert services["good"]["running"] is True
    reset_online_mode_gate()


@pytest.mark.asyncio
async def test_sync_callbacks_supported(tmp_path: Path):
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    g = configure_online_mode_gate(tmp_path / "om.json")

    started: list[str] = []
    stopped: list[str] = []

    def start_sync(): started.append("s")
    def stop_sync(): stopped.append("s")

    g.register("sync", start=start_sync, stop=stop_sync)
    await g.enable()
    await g.disable()
    assert started == ["s"]
    assert stopped == ["s"]
    reset_online_mode_gate()


@pytest.mark.asyncio
async def test_bootstrap_resumes_on_state(tmp_path: Path, monkeypatch):
    """If the persisted file says ON, bootstrap must start every
    registered service."""
    monkeypatch.delenv("HELEN_ONLINE_MODE_DEFAULT", raising=False)
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    state = tmp_path / "om.json"
    state.write_text(json.dumps({
        "enabled": True,
        "last_change_at": time.time(),
        "last_actor": "prev",
        "last_reason": "previous run",
    }))
    g = configure_online_mode_gate(state)
    started: list[str] = []
    g.register("x", start=lambda: started.append("x"), stop=lambda: None)
    await g.bootstrap()
    assert started == ["x"]
    reset_online_mode_gate()


@pytest.mark.asyncio
async def test_unregister_removes_service(tmp_path: Path):
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    g = configure_online_mode_gate(tmp_path / "om.json")
    g.register("a", start=lambda: None, stop=lambda: None)
    g.register("b", start=lambda: None, stop=lambda: None)
    g.unregister("a")
    assert {s.name for s in g._services} == {"b"}
    reset_online_mode_gate()


@pytest.mark.asyncio
async def test_history_is_capped_in_status(tmp_path: Path):
    from app.services.online_mode_gate import (
        configure_online_mode_gate, reset_online_mode_gate,
    )
    g = configure_online_mode_gate(tmp_path / "om.json")
    for i in range(40):
        await g.enable(actor=f"a{i}")
        await g.disable(actor=f"a{i}")
    snap = g.status()
    # The status() view caps the rendered history at 25 entries.
    assert len(snap["history"]) == 25
    reset_online_mode_gate()
