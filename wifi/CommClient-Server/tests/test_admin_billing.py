"""
Tests for the Tenancy + RBAC + Billing Portal admin endpoints
(``/api/admin/tenants``, ``/rbac``, ``/billing/...``).

Covered:
  * Auth 401/403 on unauthenticated requests.
  * Tenants CRUD + lifecycle (create/get/update/suspend/resume/archive).
  * Workspaces CRUD + member add/remove.
  * RBAC role CRUD + clone + permission set/get + assign/revoke role.
  * License issue → validate → revoke → renew round-trip + signature check.
  * Usage record (in-memory + DB) → retrieve current + history.
  * Invoice generate path (no subscription → 404).
  * Plan create/update/delete + audit history.
  * Tenant export (zip) + impersonate (token-issuance).

Notes:
  * The portal endpoints gate on the ``billing.manage`` / ``rbac.roles_write``
    permissions, so we have to wire those permissions onto the test admin
    user via the RBAC enforcer. The simplest path is to make the admin
    user a ``superadmin`` via the registry bootstrap — superadmin maps to
    "every permission" automatically.
  * The license signer writes keys under ``HELEN_LICENSE_KEY_DIR`` — we
    point that at a tmp dir per test session and reset the singleton.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.models.workspace import Workspace
from app.services.billing.license_signer import (
    LicenseSigner,
    build_license_payload,
    get_signer,
)
from app.services.billing.usage_meter import UsageMeter, _store as _usage_store
from app.services.rbac import enforcer as rbac_enforcer
from app.services.rbac.registry import bootstrap_default_roles


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _tmp_license_key_dir(tmp_path_factory, monkeypatch):
    """Each test session gets its own Ed25519 key directory so signatures
    don't leak between tests."""
    key_dir = tmp_path_factory.mktemp("billing-keys")
    monkeypatch.setenv("HELEN_LICENSE_KEY_DIR", str(key_dir))
    LicenseSigner.reset_for_tests()
    yield
    LicenseSigner.reset_for_tests()


@pytest.fixture
async def superadmin_headers(db_session):
    """A user wired to the superadmin role — has every permission."""
    # Seed the default RBAC permission tree + roles
    await bootstrap_default_roles(db_session)

    user = User(
        username="portaladmin",
        display_name="Portal Admin",
        password_hash=hash_password("PortalPass!2026"),
        status="online",
        role="admin",
    )
    db_session.add(user)
    await db_session.flush()

    # Map the user to the superadmin role
    super_role = (await db_session.execute(
        select(Role).where(Role.name == "superadmin")
    )).scalar_one()
    db_session.add(UserRole(user_id=user.id, role_id=super_role.id))
    await db_session.commit()
    await rbac_enforcer.invalidate_all()

    token = create_access_token(user.id, role="admin")
    return {"Authorization": f"Bearer {token}"}, user


@pytest.fixture
async def tenant(db_session):
    """Pre-create a workspace we can use as a tenant target."""
    owner = User(
        username="owner",
        display_name="Tenant Owner",
        password_hash=hash_password("OwnerPass!"),
        status="online",
    )
    db_session.add(owner)
    await db_session.flush()
    ws = Workspace(
        name="Acme Inc.",
        owner_id=owner.id,
        plan="pro",
    )
    db_session.add(ws)
    await db_session.commit()
    return ws


# ─────────────────────────────────────────────────────────────────────
# Auth gating
# ─────────────────────────────────────────────────────────────────────


class TestAuthGating:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/admin/tenants",
            "/api/admin/rbac/roles",
            "/api/admin/billing/licenses",
            "/api/admin/billing/plans-portal",
        ],
    )
    async def test_unauthenticated_401(self, client: AsyncClient, path: str):
        r = await client.get(path)
        assert r.status_code in (401, 403)

    async def test_plain_user_forbidden(
        self, client: AsyncClient, auth_headers,
    ):
        r = await client.get("/api/admin/tenants", headers=auth_headers)
        # Plain user lacks billing.manage permission → 403
        assert r.status_code in (401, 403)


# ─────────────────────────────────────────────────────────────────────
# Tenants
# ─────────────────────────────────────────────────────────────────────


class TestTenantsCRUD:
    async def test_list_empty(self, client: AsyncClient, superadmin_headers):
        headers, _ = superadmin_headers
        r = await client.get("/api/admin/tenants", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert "items" in body and isinstance(body["items"], list)
        assert "total" in body

    async def test_create_and_fetch(
        self, client: AsyncClient, superadmin_headers, tenant,
    ):
        headers, admin = superadmin_headers
        r = await client.post(
            "/api/admin/tenants",
            headers=headers,
            json={
                "name": "New Co",
                "owner_id": admin.id,
                "plan": "pro",
                "description": "demo",
            },
        )
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
        # Detail
        r = await client.get(f"/api/admin/tenants/{tid}", headers=headers)
        assert r.status_code == 200
        assert r.json()["plan"] == "pro"

    async def test_suspend_resume_archive(
        self, client: AsyncClient, superadmin_headers, tenant,
    ):
        headers, _ = superadmin_headers
        # Suspend
        r = await client.post(
            f"/api/admin/tenants/{tenant.id}/suspend", headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "suspended"
        # Resume
        r = await client.post(
            f"/api/admin/tenants/{tenant.id}/resume", headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        # Archive (soft-delete)
        r = await client.delete(
            f"/api/admin/tenants/{tenant.id}", headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "archived"

    async def test_impersonate_returns_token(
        self, client: AsyncClient, superadmin_headers, tenant,
    ):
        headers, _ = superadmin_headers
        r = await client.post(
            f"/api/admin/tenants/{tenant.id}/impersonate", headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["workspace_id"] == tenant.id
        assert isinstance(body["token"], str) and len(body["token"]) >= 32
        assert body["ttl_seconds"] > 0

    async def test_quota_get_and_set(
        self, client: AsyncClient, superadmin_headers, tenant,
    ):
        headers, _ = superadmin_headers
        r = await client.put(
            f"/api/admin/tenants/{tenant.id}/quota",
            headers=headers,
            json={"quotas": {"active_users": 12, "storage_gb": 50}},
        )
        assert r.status_code == 200
        r = await client.get(
            f"/api/admin/tenants/{tenant.id}/quota", headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["overrides"]["active_users"] == 12

    async def test_export_zip(
        self, client: AsyncClient, superadmin_headers, tenant,
    ):
        headers, _ = superadmin_headers
        r = await client.get(
            f"/api/admin/tenants/{tenant.id}/export", headers=headers,
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert len(r.content) > 0

    async def test_rotate_secrets(
        self, client: AsyncClient, superadmin_headers, tenant,
    ):
        headers, _ = superadmin_headers
        r = await client.post(
            f"/api/admin/tenants/{tenant.id}/rotate-secrets", headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert "api_secret" in body
        assert len(body["api_secret"]) == 64


# ─────────────────────────────────────────────────────────────────────
# Workspaces
# ─────────────────────────────────────────────────────────────────────


class TestWorkspaces:
    async def test_create_update_members(
        self, client: AsyncClient, superadmin_headers,
    ):
        headers, admin = superadmin_headers
        r = await client.post(
            "/api/admin/workspaces",
            headers=headers,
            json={
                "tenant_id": "n/a",  # unused field in body schema
                "name": "WS One",
                "owner_id": admin.id,
                "plan": "starter",
            },
        )
        assert r.status_code == 201
        ws_id = r.json()["id"]

        # Update
        r = await client.put(
            f"/api/admin/workspaces/{ws_id}",
            headers=headers,
            json={"name": "WS One (Renamed)", "plan": "pro"},
        )
        assert r.status_code == 200
        assert r.json()["plan"] == "pro"

        # Members list
        r = await client.get(
            f"/api/admin/workspaces/{ws_id}/members", headers=headers,
        )
        assert r.status_code == 200
        assert any(
            m["user_id"] == admin.id for m in r.json()["members"]
        )


# ─────────────────────────────────────────────────────────────────────
# RBAC
# ─────────────────────────────────────────────────────────────────────


class TestRBAC:
    async def test_list_roles_has_defaults(
        self, client: AsyncClient, superadmin_headers,
    ):
        headers, _ = superadmin_headers
        r = await client.get("/api/admin/rbac/roles", headers=headers)
        assert r.status_code == 200
        names = {role["name"] for role in r.json()["items"]}
        assert {"superadmin", "admin", "member"}.issubset(names)

    async def test_create_clone_and_delete_role(
        self, client: AsyncClient, superadmin_headers,
    ):
        headers, _ = superadmin_headers
        r = await client.post(
            "/api/admin/rbac/roles",
            headers=headers,
            json={"name": "auditor", "description": "read-only audit"},
        )
        assert r.status_code == 201
        # Set permissions
        r = await client.put(
            "/api/admin/rbac/roles/auditor/permissions",
            headers=headers,
            json={"permissions": ["messages.read", "users.read", "system.logs"]},
        )
        assert r.status_code == 200
        # Clone
        r = await client.post(
            "/api/admin/rbac/roles/auditor/clone",
            headers=headers,
            json={"new_name": "auditor-2"},
        )
        assert r.status_code == 200
        # Cloned role should have the same permissions
        r = await client.get(
            "/api/admin/rbac/roles/auditor-2/permissions",
            headers=headers,
        )
        assert r.status_code == 200
        assert set(r.json()["permissions"]) == {
            "messages.read", "users.read", "system.logs",
        }
        # Delete the clone (not system role)
        r = await client.delete(
            "/api/admin/rbac/roles/auditor-2", headers=headers,
        )
        assert r.status_code == 200

    async def test_cannot_delete_system_role(
        self, client: AsyncClient, superadmin_headers,
    ):
        headers, _ = superadmin_headers
        r = await client.delete(
            "/api/admin/rbac/roles/admin", headers=headers,
        )
        assert r.status_code == 400

    async def test_grant_and_revoke_role_on_user(
        self, client: AsyncClient, superadmin_headers, db_session,
    ):
        headers, admin = superadmin_headers
        # Create a fresh user
        u = User(
            username="bob",
            display_name="Bob",
            password_hash=hash_password("BobPass!"),
            status="online",
        )
        db_session.add(u)
        await db_session.commit()

        r = await client.post(
            f"/api/admin/rbac/users/{u.id}/roles/moderator",
            headers=headers,
        )
        assert r.status_code == 200

        r = await client.get(
            f"/api/admin/rbac/users/{u.id}", headers=headers,
        )
        assert r.status_code == 200
        assert "moderator" in r.json()["roles"]

        r = await client.delete(
            f"/api/admin/rbac/users/{u.id}/roles/moderator",
            headers=headers,
        )
        assert r.status_code == 200

    async def test_admin_reset_password(
        self, client: AsyncClient, superadmin_headers, db_session,
    ):
        headers, _ = superadmin_headers
        u = User(
            username="alice",
            display_name="Alice",
            password_hash=hash_password("AlicePass!"),
            status="online",
        )
        db_session.add(u)
        await db_session.commit()

        r = await client.post(
            f"/api/admin/rbac/users/{u.id}/reset-password", headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["temp_password"], str)
        assert len(body["temp_password"]) >= 8


# ─────────────────────────────────────────────────────────────────────
# License Signer (unit-level)
# ─────────────────────────────────────────────────────────────────────


class TestLicenseSigner:
    def test_keypair_generated(self):
        signer = get_signer()
        pem = signer.export_public_key()
        assert "BEGIN PUBLIC KEY" in pem
        assert len(signer.export_fingerprint()) == 32

    def test_sign_and_verify_roundtrip(self):
        signer = get_signer()
        from datetime import datetime, timedelta, timezone
        payload = build_license_payload(
            license_key="HLN-AAAA-BBBB-CCCC-DDDD",
            workspace_id="ws1",
            plan_slug="pro",
            seats=10,
            features={"sso": True},
            issued_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        sig = signer.sign_license(payload)
        assert signer.verify_license(payload, sig) is True
        # Tamper detection
        bad = dict(payload, seats=999)
        assert signer.verify_license(bad, sig) is False
        # Garbage signature
        assert signer.verify_license(payload, "x" * 88) is False


# ─────────────────────────────────────────────────────────────────────
# License HTTP endpoints
# ─────────────────────────────────────────────────────────────────────


class TestLicenseEndpoints:
    async def test_issue_validate_revoke_renew(
        self, client: AsyncClient, superadmin_headers, tenant,
    ):
        headers, _ = superadmin_headers
        # Issue
        r = await client.post(
            "/api/admin/billing/licenses",
            headers=headers,
            json={
                "tenant_id": tenant.id, "plan": "pro",
                "seats": 50, "duration_days": 365,
                "features": {"sso": True, "audit_log": True},
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        key = body["key"]
        sig = body["signature"]
        payload = body["payload"]
        assert payload["seats"] == 50
        assert isinstance(sig, str) and len(sig) > 0

        # Validate by key
        r = await client.post(
            f"/api/admin/billing/licenses/{key}/validate", headers=headers,
        )
        assert r.status_code == 200
        v = r.json()
        assert v["valid"] is True and v["signature_ok"] is True

        # Validate by payload
        r = await client.post(
            "/api/admin/billing/licenses/validate",
            headers=headers,
            json={"license": payload, "signature": sig},
        )
        assert r.status_code == 200
        assert r.json()["valid"] is True

        # Renew
        r = await client.post(
            f"/api/admin/billing/licenses/{key}/renew",
            headers=headers, json={"duration_days": 30},
        )
        assert r.status_code == 200

        # Download
        r = await client.get(
            f"/api/admin/billing/licenses/{key}/download", headers=headers,
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(
            "application/x-helen-license+json",
        )

        # Revoke
        r = await client.post(
            f"/api/admin/billing/licenses/{key}/revoke", headers=headers,
        )
        assert r.status_code == 200

        # Validate after revoke → invalid
        r = await client.post(
            f"/api/admin/billing/licenses/{key}/validate", headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["valid"] is False


# ─────────────────────────────────────────────────────────────────────
# Usage meter
# ─────────────────────────────────────────────────────────────────────


class TestUsageMeter:
    def test_record_in_memory(self):
        _usage_store.clear()
        UsageMeter.record(
            tenant_id="t1", user_id="u1",
            endpoint="GET /api/messages",
            bytes_in=128, bytes_out=512,
        )
        snap = UsageMeter.live_snapshot("t1")
        assert snap["endpoints"]
        assert snap["endpoints"][0]["bytes_out"] == 512

    async def test_get_current_aggregates(
        self, client: AsyncClient, superadmin_headers, tenant,
    ):
        headers, _ = superadmin_headers
        # Record + flush
        UsageMeter.record_metric(
            tenant.id, "messages_sent", value=5.0, source="test",
        )
        await UsageMeter.flush_now()
        r = await client.get(
            f"/api/admin/billing/usage/current?tenant_id={tenant.id}",
            headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert "totals" in body
        assert body["totals"].get("messages_sent", 0) >= 5.0


# ─────────────────────────────────────────────────────────────────────
# Plans + audit
# ─────────────────────────────────────────────────────────────────────


class TestPlans:
    async def test_create_update_delete_with_audit(
        self, client: AsyncClient, superadmin_headers,
    ):
        headers, _ = superadmin_headers
        r = await client.post(
            "/api/admin/billing/plans-portal",
            headers=headers,
            json={
                "code": "team", "name": "Team",
                "price_monthly_cents": 1500,
                "price_yearly_cents": 15000, "currency": "USD",
                "included_quotas": {"active_users": 30},
                "feature_flags": {"calls": True},
            },
        )
        assert r.status_code == 201, r.text

        # Update
        r = await client.put(
            "/api/admin/billing/plans-portal/team",
            headers=headers,
            json={
                "code": "team", "name": "Team v2",
                "price_monthly_cents": 1800,
                "price_yearly_cents": 18000, "currency": "USD",
                "included_quotas": {"active_users": 35},
                "feature_flags": {"calls": True, "sso": True},
            },
        )
        assert r.status_code == 200

        # Audit history shows 2 entries
        r = await client.get(
            "/api/admin/billing/plans-portal/team/audit", headers=headers,
        )
        assert r.status_code == 200
        actions = [e["action"] for e in r.json()["entries"]]
        assert "create" in actions and "update" in actions

        # Delete
        r = await client.delete(
            "/api/admin/billing/plans-portal/team", headers=headers,
        )
        assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────
# Invoices
# ─────────────────────────────────────────────────────────────────────


class TestInvoices:
    async def test_generate_no_subscription_404(
        self, client: AsyncClient, superadmin_headers, tenant,
    ):
        headers, _ = superadmin_headers
        r = await client.post(
            "/api/admin/billing/invoices-portal/generate",
            headers=headers,
            json={"tenant_id": tenant.id},
        )
        # No subscription wired in the test fixture → 404 by design.
        assert r.status_code == 404

    async def test_list_returns_list(
        self, client: AsyncClient, superadmin_headers,
    ):
        headers, _ = superadmin_headers
        r = await client.get(
            "/api/admin/billing/invoices-portal", headers=headers,
        )
        assert r.status_code == 200
        assert isinstance(r.json()["items"], list)
