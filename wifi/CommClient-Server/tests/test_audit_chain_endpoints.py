"""
Tests for the admin audit-chain endpoints + the chain-via-audit_log hook.
"""

from __future__ import annotations

import pytest

from app.core.audit import audit_log
from app.services.audit_chain import configure_audit_chain, get_audit_chain


@pytest.fixture
def fresh_chain(tmp_path):
    """Reset the module-level singleton to a temp DB so tests are isolated."""
    import app.services.audit_chain as mod
    mod._CHAIN = None  # noqa: SLF001
    chain = configure_audit_chain(str(tmp_path / "audit_chain.db"))
    yield chain
    mod._CHAIN = None


@pytest.mark.asyncio
async def test_head_404_when_empty_then_populated(client, admin_headers, fresh_chain):
    r = await client.get("/api/admin/audit-chain/head", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body.get("empty") is True

    audit_log("test.action", user_id="alice", details={"target": "ch1"})

    r2 = await client.get("/api/admin/audit-chain/head", headers=admin_headers)
    assert r2.status_code == 200
    head = r2.json()["head"]
    assert head["actor"] == "alice"
    assert head["action"] == "test.action"


@pytest.mark.asyncio
async def test_verify_endpoint_reports_intact(client, admin_headers, fresh_chain):
    audit_log("login", user_id="alice")
    audit_log("login", user_id="bob")
    r = await client.post("/api/admin/audit-chain/verify", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["broken_at_seq"] is None


@pytest.mark.asyncio
async def test_filter_entries_by_actor(client, admin_headers, fresh_chain):
    audit_log("login", user_id="alice")
    audit_log("login", user_id="bob")
    audit_log("logout", user_id="alice")

    r = await client.get(
        "/api/admin/audit-chain/entries?actor=alice",
        headers=admin_headers,
    )
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert all(e["actor"] == "alice" for e in entries)
    assert len(entries) >= 2


@pytest.mark.asyncio
async def test_chain_detects_tampering(fresh_chain):
    """Direct chain test — bypasses HTTP. If anyone EDITS the SQLite
    table, verify() must catch it."""
    audit_log("event1", user_id="alice")
    audit_log("event2", user_id="bob")

    chain = get_audit_chain()
    ok, broken_at, msg = chain.verify()
    assert ok, msg

    # Tamper directly with the SQLite store.
    import sqlite3
    with sqlite3.connect(chain.db_path) as c:
        c.execute(
            "UPDATE audit_chain SET payload_json = '{\"forged\":true}' "
            "WHERE seq = 1"
        )

    ok, broken_at, msg = chain.verify()
    assert not ok
    assert broken_at == 1
    assert "payload_hash mismatch" in msg or "chain_hash mismatch" in msg


@pytest.mark.asyncio
async def test_admin_only(client, auth_headers):
    """Non-admin users must get 403."""
    r = await client.get("/api/admin/audit-chain/head", headers=auth_headers)
    assert r.status_code == 403
    r2 = await client.post("/api/admin/audit-chain/verify", headers=auth_headers)
    assert r2.status_code == 403
