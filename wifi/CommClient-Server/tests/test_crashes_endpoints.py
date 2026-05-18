"""
Tests for admin /api/admin/crashes/* — backed by services/crash_reporter.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def fresh_reporter(tmp_path):
    """Reset the module-level singleton + install a fresh crash reporter
    against an isolated temp DB."""
    import app.services.crash_reporter as mod
    mod._REPORTER = None  # noqa: SLF001
    rep = mod.install_crash_reporter(str(tmp_path), helen_version="test")
    yield rep
    mod._REPORTER = None


@pytest.mark.asyncio
async def test_list_empty(client, admin_headers, fresh_reporter):
    r = await client.get("/api/admin/crashes", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] is True
    assert body["events"] == []


@pytest.mark.asyncio
async def test_capture_then_list(client, admin_headers, fresh_reporter):
    try:
        raise RuntimeError("synthetic crash")
    except RuntimeError as exc:
        eid = fresh_reporter.capture_exception(exc, level="error")

    r = await client.get("/api/admin/crashes", headers=admin_headers)
    assert r.status_code == 200
    events = r.json()["events"]
    assert any(e["event_id"] == eid for e in events)
    rec = next(e for e in events if e["event_id"] == eid)
    assert rec["type"] == "RuntimeError"
    assert "synthetic crash" in rec["message"]


@pytest.mark.asyncio
async def test_get_specific(client, admin_headers, fresh_reporter):
    eid = fresh_reporter.capture_event("warning", "test event", foo="bar")
    r = await client.get(f"/api/admin/crashes/{eid}", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["event_id"] == eid
    assert body["level"] == "warning"
    assert body["context"]["foo"] == "bar"


@pytest.mark.asyncio
async def test_purge_older_than(client, admin_headers, fresh_reporter):
    fresh_reporter.capture_event("info", "event 1")
    fresh_reporter.capture_event("info", "event 2")
    # purge with a huge "days" so nothing is older — 0 deletes
    r = await client.delete("/api/admin/crashes/older-than/365", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["deleted"] == 0


@pytest.mark.asyncio
async def test_admin_only(client, auth_headers):
    r = await client.get("/api/admin/crashes", headers=auth_headers)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_redacts_secrets_from_context(fresh_reporter):
    """Verify the in-process redaction; token/password keys must be masked."""
    eid = fresh_reporter.capture_event(
        "info", "with secrets",
        password="hunter2",
        jwt_token="abc",
        normal_field="ok",
    )
    rec = fresh_reporter.store.get(eid)
    assert rec["context"]["password"] == "<redacted>"
    assert rec["context"]["jwt_token"] == "<redacted>"
    assert rec["context"]["normal_field"] == "ok"
