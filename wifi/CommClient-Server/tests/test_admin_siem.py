"""
Integration tests for the admin SIEM / Audit Chain dashboard.

These tests stand up a minimal FastAPI app that mounts ONLY the
``admin_siem`` router. The audit chain singleton is bound to a
temp-directory SQLite file, ensuring tests are hermetic.

Coverage
--------
* /head — returns head info with verify status
* /entries — paginated entry list, filters, severity inference
* /verify — sync + async paths + /verify/jobs/{id} status
* /stats — totals, by_severity, by_action, by_actor_top10
* /actors/suggest — autocomplete
* /rules CRUD + DSL validation + /test dry-run + enable/disable
* /holds CRUD + conflict detection + release
* /retention CRUD + preview + apply (dry_run)
* WebSocket — connect with auth + receive simulated entry
* Auth — 401 without token
* DSL — parser unit tests for AND/OR/NOT/IN/WITHIN
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── App factory + helpers ────────────────────────────────────────────────


@pytest.fixture(scope="function")
def temp_chain_db(tmp_path, monkeypatch):
    """Bind the audit chain singleton to a fresh SQLite file for each
    test and reset module-level caches."""
    import app.services.audit_chain as chain_mod
    import app.services.audit.chain as siem_chain
    import app.services.audit.alert_rules as rules_mod
    import app.services.audit.ws_stream as ws_mod
    import app.services.audit.export_engine as exp_mod

    # Reset chain singleton
    chain_mod._CHAIN = None
    db_path = str(tmp_path / "audit_chain.db")
    chain_mod.configure_audit_chain(db_path)

    # Reset SIEM caches
    rules_mod._engine_singleton = None
    rules_mod._within_state.windows.clear()
    ws_mod._manager = None
    exp_mod._engine = None
    siem_chain._subscribers.clear()
    siem_chain._patched = False
    siem_chain._ensure_patched()

    yield db_path


def _make_app() -> FastAPI:
    from app.api.routes.admin_siem import router as siem_router
    app = FastAPI()
    app.include_router(siem_router, prefix="/api")
    return app


def _admin_headers() -> dict[str, str]:
    from app.core.security import create_access_token
    tok = create_access_token("admin-test", role="admin")
    return {"Authorization": f"Bearer {tok}"}


def _admin_token() -> str:
    from app.core.security import create_access_token
    return create_access_token("admin-test", role="admin")


@pytest.fixture
def client(temp_chain_db):
    app = _make_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def headers():
    return _admin_headers()


def _seed_entries(n: int = 5, actor: str = "alice") -> None:
    from app.services.audit_chain import get_audit_chain
    chain = get_audit_chain()
    assert chain is not None
    for i in range(n):
        chain.append(
            actor=actor,
            action=f"test.event.{i}",
            target=f"resource-{i % 3}",
            payload={"i": i, "ok": True},
        )


# ── /head ────────────────────────────────────────────────────────────────


def test_head_empty_chain(client, headers):
    r = client.get("/api/admin/audit/head", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["index"] == 0
    assert "verify_status" in data


def test_head_with_entries(client, headers):
    _seed_entries(3)
    r = client.get("/api/admin/audit/head?verify=true", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["index"] == 3
    assert data["verify_status"] == "ok"
    assert data["entry_hash"]


# ── /entries ─────────────────────────────────────────────────────────────


def test_entries_returns_paginated_results(client, headers):
    _seed_entries(7)
    r = client.get("/api/admin/audit/entries?limit=3", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["entries"]) == 3
    assert data["total"] == 7
    # All entries should have severity derived
    for e in data["entries"]:
        assert "severity" in e


def test_entries_filter_by_actor(client, headers):
    _seed_entries(3, actor="bob")
    _seed_entries(2, actor="alice")
    r = client.get("/api/admin/audit/entries?actor=bob", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert all(e["actor"] == "bob" for e in data["entries"])


# ── /verify ──────────────────────────────────────────────────────────────


def test_verify_sync(client, headers):
    _seed_entries(5)
    r = client.post("/api/admin/audit/verify", json={}, headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["broken_at_index"] is None


def test_verify_async_job(client, headers):
    _seed_entries(2)
    r = client.post("/api/admin/audit/verify",
                    json={"async_run": True}, headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["async"] is True
    job_id = data["job_id"]

    # Poll a few times for completion
    for _ in range(20):
        time.sleep(0.05)
        s = client.get(f"/api/admin/audit/verify/jobs/{job_id}",
                       headers=headers).json()
        if s["status"] in ("ready", "failed"):
            break
    assert s["status"] == "ready"
    assert s["ok"] is True


# ── /stats ───────────────────────────────────────────────────────────────


def test_stats_distributions(client, headers):
    _seed_entries(4, actor="alice")
    _seed_entries(2, actor="bob")
    r = client.get("/api/admin/audit/stats", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 6
    assert "by_severity" in data
    actor_keys = {a["key"] for a in data["by_actor_top10"]}
    assert {"alice", "bob"} <= actor_keys


# ── /actors/suggest ──────────────────────────────────────────────────────


def test_actors_suggest(client, headers):
    _seed_entries(1, actor="alice")
    _seed_entries(1, actor="alpha")
    _seed_entries(1, actor="bob")
    r = client.get("/api/admin/audit/actors/suggest?q=al", headers=headers)
    assert r.status_code == 200
    s = r.json()["suggestions"]
    assert "alice" in s and "alpha" in s
    assert "bob" not in s


# ── Auth ─────────────────────────────────────────────────────────────────


def test_unauth_returns_401(client):
    r = client.get("/api/admin/audit/head")
    assert r.status_code in (401, 403)


def test_wrong_role_returns_403(client):
    from app.core.security import create_access_token
    tok = create_access_token("user1", role="user")
    r = client.get("/api/admin/audit/head",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


# ── Rules CRUD ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rules_crud_and_dry_run(client, headers):
    # Create
    r = client.post("/api/admin/audit/rules", headers=headers, json={
        "name": "login-failures",
        "description": "watch failed logins",
        "condition_dsl": "action = \"auth.login\" AND payload.success = false",
        "severity": "high",
        "channels": ["local", "email"],
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    # List
    r = client.get("/api/admin/audit/rules", headers=headers)
    assert any(rule["id"] == rid for rule in r.json()["rules"])

    # Update
    r = client.put(f"/api/admin/audit/rules/{rid}", headers=headers, json={
        "severity": "critical",
    })
    assert r.status_code == 200
    assert r.json()["severity"] == "critical"

    # Test (dry-run)
    _seed_entries(3)
    r = client.post(f"/api/admin/audit/rules/{rid}/test", headers=headers)
    assert r.status_code == 200
    assert "scanned" in r.json()

    # Disable
    r = client.post(f"/api/admin/audit/rules/{rid}/disable", headers=headers)
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    # Delete
    r = client.delete(f"/api/admin/audit/rules/{rid}", headers=headers)
    assert r.status_code == 200


def test_rules_bad_dsl_rejected(client, headers):
    r = client.post("/api/admin/audit/rules", headers=headers, json={
        "name": "bad-rule",
        "condition_dsl": "this is === not valid",
        "severity": "low",
        "channels": ["local"],
    })
    assert r.status_code == 400


# ── Legal holds ──────────────────────────────────────────────────────────


def test_holds_crud_and_conflict(client, headers):
    # Create first hold
    r = client.post("/api/admin/audit/holds", headers=headers, json={
        "name": "case-001",
        "case_ref": "LEG-001",
        "scope": {"actors": ["alice"]},
    })
    assert r.status_code == 201, r.text
    hid = r.json()["id"]

    # Conflict — same actor scope
    r = client.post("/api/admin/audit/holds", headers=headers, json={
        "name": "case-002",
        "scope": {"actors": ["alice"]},
    })
    assert r.status_code == 409

    # Force succeeds
    r = client.post("/api/admin/audit/holds", headers=headers, json={
        "name": "case-002",
        "scope": {"actors": ["alice"]},
        "force": True,
    })
    assert r.status_code == 201

    # Release without confirmation
    r = client.post(f"/api/admin/audit/holds/{hid}/release", headers=headers,
                    json={"reason": "investigation closed",
                          "confirmation": "case-001"})
    assert r.status_code == 200
    assert r.json()["status"] == "released"


def test_holds_release_bad_confirmation(client, headers):
    r = client.post("/api/admin/audit/holds", headers=headers, json={
        "name": "case-003",
        "scope": {"actors": ["bob"]},
    })
    hid = r.json()["id"]
    r = client.post(f"/api/admin/audit/holds/{hid}/release", headers=headers,
                    json={"reason": "x", "confirmation": "WRONG"})
    assert r.status_code == 400


# ── Retention ────────────────────────────────────────────────────────────


def test_retention_crud_preview(client, headers):
    r = client.post("/api/admin/audit/retention/policies", headers=headers, json={
        "name": "audit-90d",
        "resource_type": "audit_chain",
        "period_days": 90,
        "action": "archive",
        "exemptions": {"holds": True},
    })
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = client.get("/api/admin/audit/retention/policies", headers=headers)
    assert any(p["id"] == pid for p in r.json()["policies"])

    # Preview (won't match anything because the seeded entries are now)
    _seed_entries(2)
    r = client.post(f"/api/admin/audit/retention/policies/{pid}/preview",
                    headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["affected"] == 0  # nothing older than 90 days

    # Apply dry-run
    r = client.post(
        f"/api/admin/audit/retention/policies/{pid}/apply?dry_run=true",
        headers=headers,
    )
    assert r.status_code == 200


def test_retention_invalid_action(client, headers):
    r = client.post("/api/admin/audit/retention/policies", headers=headers, json={
        "name": "bad-retention",
        "resource_type": "audit_chain",
        "period_days": 30,
        "action": "shred",  # not in VALID_RETENTION_ACTIONS
    })
    assert r.status_code == 400


# ── WebSocket ────────────────────────────────────────────────────────────


def test_websocket_no_token_rejected(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/api/admin/audit/ws") as ws:
            ws.receive_text()


def test_websocket_receives_entry(client):
    tok = _admin_token()
    with client.websocket_connect(
        f"/api/admin/audit/ws?token={tok}"
    ) as ws:
        # Trigger an entry append from the same loop
        from app.services.audit_chain import get_audit_chain
        get_audit_chain().append(
            actor="alice", action="test.ws.event",
            target="r1", payload={"x": 1},
        )
        msg = ws.receive_text()
        data = json.loads(msg)
        # Could be a ping if subscriber hook missed; loop until entry
        if data.get("type") == "ping":
            data = json.loads(ws.receive_text())
        assert data["type"] in ("entry", "alert")


# ── DSL parser unit tests ────────────────────────────────────────────────


def test_dsl_basic_and():
    from app.services.audit.alert_rules import parse_dsl, _evaluate
    from app.services.audit.chain import AuditEntry
    ast = parse_dsl('actor = "alice" AND action = "test.x"')
    e = AuditEntry(seq=1, timestamp=0, actor="alice", action="test.x",
                   target=None, payload={})
    assert _evaluate(ast, e) is True
    e2 = AuditEntry(seq=2, timestamp=0, actor="bob", action="test.x",
                    target=None, payload={})
    assert _evaluate(ast, e2) is False


def test_dsl_in_and_not():
    from app.services.audit.alert_rules import parse_dsl, _evaluate
    from app.services.audit.chain import AuditEntry
    ast = parse_dsl('action IN ["a", "b"] AND NOT actor = "system"')
    e = AuditEntry(seq=1, timestamp=0, actor="alice", action="a",
                   target=None, payload={})
    assert _evaluate(ast, e) is True
    e2 = AuditEntry(seq=2, timestamp=0, actor="system", action="a",
                    target=None, payload={})
    assert _evaluate(ast, e2) is False


def test_dsl_severity_compare():
    from app.services.audit.alert_rules import parse_dsl, _evaluate
    from app.services.audit.chain import AuditEntry
    ast = parse_dsl('severity >= "high"')
    e = AuditEntry(seq=1, timestamp=0, actor="x", action="delete",
                   target=None, payload={})
    assert _evaluate(ast, e) is True


def test_dsl_within_rate():
    from app.services.audit.alert_rules import parse_dsl, _evaluate
    from app.services.audit.chain import AuditEntry
    ast = parse_dsl('WITHIN 60s OF (action = "auth.fail")')
    base = time.time()
    e1 = AuditEntry(seq=1, timestamp=base, actor="x", action="auth.fail",
                    target=None, payload={})
    e2 = AuditEntry(seq=2, timestamp=base + 5, actor="x", action="auth.fail",
                    target=None, payload={})
    # First hit doesn't trigger (threshold=2), second does
    assert _evaluate(ast, e1) is False
    assert _evaluate(ast, e2) is True


def test_dsl_invalid_raises():
    from app.services.audit.alert_rules import parse_dsl, DSLError
    with pytest.raises(DSLError):
        parse_dsl("actor === unknown")


# ── Legal hold scope matching ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_legal_hold_is_under_hold(temp_chain_db):
    from app.services.audit.legal_hold import get_legal_hold_service
    svc = get_legal_hold_service()
    await svc.create(
        name="hold-x", scope={"actors": ["alice"]},
        actor_id="admin-test",
    )
    assert await svc.is_under_hold(
        resource_type="audit_chain", actor="alice",
    ) is True
    assert await svc.is_under_hold(
        resource_type="audit_chain", actor="bob",
    ) is False
