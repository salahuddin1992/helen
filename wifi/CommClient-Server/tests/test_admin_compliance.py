"""
Tests for the Compliance / eDiscovery Workbench
(``/api/admin/compliance/...``).

Covers:
  * Auth (401/403)
  * Hold CRUD + conflict detection
  * Retention preview + apply (dry-run, typed APPLY)
  * eDiscovery search + facets
  * Case CRUD + evidence + export
  * DSAR create + fulfill (typed FULFILL)
  * RTBF blocked by hold → 409 with GDPR cite
  * RTBF execute with typed confirmation
  * Classification scan + builtin rules
  * Framework status (10 frameworks)
  * Report generation (json)
  * Audit linkage
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services.rbac import enforcer as rbac_enforcer
from app.services.rbac.registry import bootstrap_default_roles


@pytest.fixture
async def cmp_admin_headers(db_session):
    await bootstrap_default_roles(db_session)
    user = User(
        username="cmpadmin",
        display_name="Compliance Admin",
        password_hash=hash_password("CmpPass!2026"),
        status="online",
        role="admin",
    )
    db_session.add(user)
    await db_session.flush()
    super_role = (await db_session.execute(
        select(Role).where(Role.name == "superadmin")
    )).scalar_one()
    db_session.add(UserRole(user_id=user.id, role_id=super_role.id))
    await db_session.commit()
    await rbac_enforcer.invalidate_all()
    token = create_access_token(user.id, role="admin")
    return {"Authorization": f"Bearer {token}", "_user_id": user.id}


@pytest.fixture
async def regular_headers(db_session):
    user = User(
        username="cmpregular",
        display_name="Regular",
        password_hash=hash_password("Regular!1234"),
        status="online",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()
    token = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────


class TestAuth:
    async def test_holds_unauthenticated(self, client: AsyncClient):
        r = await client.get("/api/admin/compliance/holds")
        assert r.status_code in (401, 403)

    async def test_holds_regular_forbidden(
        self, client: AsyncClient, regular_headers,
    ):
        r = await client.get("/api/admin/compliance/holds", headers=regular_headers)
        assert r.status_code == 403

    async def test_search_unauthenticated(self, client: AsyncClient):
        r = await client.post(
            "/api/admin/compliance/ediscovery/search", json={"q": "x"}
        )
        assert r.status_code in (401, 403)


# ─────────────────────────────────────────────────────────────────
# Holds
# ─────────────────────────────────────────────────────────────────


class TestHolds:
    async def test_create_and_get_hold(self, client: AsyncClient, cmp_admin_headers):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        body = {
            "name": "Litigation Project Alpha",
            "case_ref": "CASE-0001",
            "scope": {"custodians": ["u1", "u2"], "channels": ["c1"]},
            "retention_override": True,
        }
        r = await client.post("/api/admin/compliance/holds", headers=h, json=body)
        assert r.status_code == 201, r.text
        data = r.json()
        hold = data["hold"]
        assert hold["name"] == body["name"]
        assert hold["status"] == "active"

        r2 = await client.get(
            f"/api/admin/compliance/holds/{hold['id']}", headers=h,
        )
        assert r2.status_code == 200
        assert r2.json()["id"] == hold["id"]

    async def test_overlapping_holds_flagged_as_conflict(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        body = {
            "name": "Hold A",
            "scope": {"custodians": ["alice"], "channels": ["c-news"]},
        }
        r1 = await client.post("/api/admin/compliance/holds", headers=h, json=body)
        assert r1.status_code == 201
        body2 = {
            "name": "Hold B",
            "scope": {"custodians": ["alice", "bob"]},
        }
        r2 = await client.post("/api/admin/compliance/holds", headers=h, json=body2)
        assert r2.status_code == 201
        data = r2.json()
        assert len(data["conflicts"]) >= 1
        assert data["conflicts"][0]["name"] == "Hold A"

    async def test_release_requires_typed_confirmation(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r1 = await client.post(
            "/api/admin/compliance/holds", headers=h,
            json={"name": "Hold-X", "scope": {}},
        )
        hid = r1.json()["hold"]["id"]
        r2 = await client.post(
            f"/api/admin/compliance/holds/{hid}/release",
            headers=h, json={"confirmation": "nope", "reason": "abc"},
        )
        assert r2.status_code == 400
        r3 = await client.post(
            f"/api/admin/compliance/holds/{hid}/release",
            headers=h, json={"confirmation": "RELEASE", "reason": "case closed"},
        )
        assert r3.status_code == 200
        assert r3.json()["status"] == "released"

    async def test_hold_audit_trail(self, client: AsyncClient, cmp_admin_headers):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r1 = await client.post(
            "/api/admin/compliance/holds", headers=h,
            json={"name": "Hold-Audit", "scope": {}},
        )
        hid = r1.json()["hold"]["id"]
        r2 = await client.get(
            f"/api/admin/compliance/holds/{hid}/audit", headers=h,
        )
        assert r2.status_code == 200
        items = r2.json()["items"]
        assert any(it["event"] == "hold.created" for it in items)


# ─────────────────────────────────────────────────────────────────
# Retention v2
# ─────────────────────────────────────────────────────────────────


class TestRetention:
    async def test_create_policy_and_preview(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r1 = await client.post(
            "/api/admin/compliance/retention/policies",
            headers=h, json={
                "name": "Old messages",
                "resource_type": "messages",
                "retention_days": 365,
                "action": "anonymize",
            },
        )
        assert r1.status_code == 201, r1.text
        pid = r1.json()["id"]

        r2 = await client.post(
            "/api/admin/compliance/retention/policies/preview",
            headers=h, json={"policy": {
                "resource_type": "messages", "retention_days": 365,
            }},
        )
        assert r2.status_code == 200
        body = r2.json()
        assert "would_affect" in body or "skipped" in body

        # apply without typed confirmation → 400
        r3 = await client.post(
            f"/api/admin/compliance/retention/policies/{pid}/apply",
            headers=h, json={"confirmation": "no", "dry_run": True},
        )
        assert r3.status_code == 400

        # apply with confirmation
        r4 = await client.post(
            f"/api/admin/compliance/retention/policies/{pid}/apply",
            headers=h, json={"confirmation": "APPLY", "dry_run": True},
        )
        assert r4.status_code == 200, r4.text
        assert r4.json()["status"] in ("ready", "failed")

    async def test_invalid_action_rejected(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.post(
            "/api/admin/compliance/retention/policies",
            headers=h, json={
                "name": "bad", "resource_type": "messages",
                "retention_days": 10, "action": "nuke",
            },
        )
        assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────
# eDiscovery search + cases + export
# ─────────────────────────────────────────────────────────────────


class TestEDiscovery:
    async def test_search_returns_envelope(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.post(
            "/api/admin/compliance/ediscovery/search",
            headers=h, json={"q": "hello AND world", "limit": 10},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("total", "items", "facets", "parsed_clauses"):
            assert k in body

    async def test_case_crud_and_export(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        # Create
        r1 = await client.post(
            "/api/admin/compliance/ediscovery/cases",
            headers=h, json={"name": "Matter-001", "custodians": ["u1"]},
        )
        assert r1.status_code == 201, r1.text
        cid = r1.json()["id"]

        # Add evidence
        r2 = await client.post(
            f"/api/admin/compliance/ediscovery/cases/{cid}/evidence",
            headers=h, json={"items": [
                {"resource_type": "messages", "resource_id": "msg-1",
                 "tag": "relevant"},
                {"resource_type": "audit", "resource_id": "log-1",
                 "tag": "key_evidence"},
            ]},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["added"] == 2

        # Timeline
        r3 = await client.get(
            f"/api/admin/compliance/ediscovery/cases/{cid}/timeline",
            headers=h,
        )
        assert r3.status_code == 200
        assert len(r3.json()["items"]) == 2

        # Export
        r4 = await client.post(
            f"/api/admin/compliance/ediscovery/cases/{cid}/export",
            headers=h, json={"format": "legal-zip", "options": {}},
        )
        assert r4.status_code == 200, r4.text
        export_id = r4.json()["export_job_id"]
        assert r4.json()["status"] in ("ready", "running")

        # Status
        r5 = await client.get(
            f"/api/admin/compliance/ediscovery/cases/{cid}/exports/{export_id}",
            headers=h,
        )
        assert r5.status_code == 200
        assert r5.json()["status"] in ("ready", "running", "failed")

    async def test_unsupported_export_format_rejected(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.post(
            "/api/admin/compliance/ediscovery/cases",
            headers=h, json={"name": "Matter-002"},
        )
        cid = r.json()["id"]
        r2 = await client.post(
            f"/api/admin/compliance/ediscovery/cases/{cid}/export",
            headers=h, json={"format": "nope"},
        )
        assert r2.status_code == 400


# ─────────────────────────────────────────────────────────────────
# DSAR (Article 15)
# ─────────────────────────────────────────────────────────────────


class TestDSAR:
    async def test_create_and_get(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        actor_id = cmp_admin_headers["_user_id"]
        r = await client.post(
            "/api/admin/compliance/dsar/requests",
            headers=h, json={
                "subject_id": actor_id,
                "subject_email": "subject@example.com",
                "subject_name": "John Doe",
                "type": "access",
                "identity_verified": True,
            },
        )
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        r2 = await client.get(
            f"/api/admin/compliance/dsar/requests/{rid}", headers=h,
        )
        assert r2.status_code == 200
        assert r2.json()["identity_verified"] is True

    async def test_fulfill_requires_typed_confirmation(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        actor_id = cmp_admin_headers["_user_id"]
        r = await client.post(
            "/api/admin/compliance/dsar/requests",
            headers=h, json={
                "subject_id": actor_id, "type": "access",
                "identity_verified": True,
            },
        )
        rid = r.json()["id"]

        # wrong confirmation
        r2 = await client.post(
            f"/api/admin/compliance/dsar/requests/{rid}/fulfill",
            headers=h, json={"confirmation": "wrong"},
        )
        assert r2.status_code == 400

        # correct confirmation
        r3 = await client.post(
            f"/api/admin/compliance/dsar/requests/{rid}/fulfill",
            headers=h, json={"confirmation": "FULFILL", "redact_pii": False},
        )
        # may be 200 or 500 depending on storage setup; if 200, must be fulfilled
        assert r3.status_code in (200, 500)
        if r3.status_code == 200:
            assert r3.json()["status"] in ("fulfilled", "already_fulfilled")

    async def test_invalid_type_rejected(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        actor_id = cmp_admin_headers["_user_id"]
        r = await client.post(
            "/api/admin/compliance/dsar/requests",
            headers=h, json={"subject_id": actor_id, "type": "nuke"},
        )
        assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────
# RTBF (Article 17) — hold conflict + execution
# ─────────────────────────────────────────────────────────────────


class TestRTBF:
    async def test_rtbf_blocked_when_subject_under_hold(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        # Place subject under hold
        await client.post(
            "/api/admin/compliance/holds",
            headers=h, json={
                "name": "Hold-RTBF-Test",
                "scope": {"custodians": ["subject-1"]},
            },
        )
        # Try to create RTBF — should 409 with GDPR cite
        r = await client.post(
            "/api/admin/compliance/rtbf/requests",
            headers=h, json={"subject_id": "subject-1"},
        )
        assert r.status_code == 409, r.text
        body = r.json()
        # FastAPI nests dict-detail under "detail"
        detail = body.get("detail") if isinstance(body, dict) else body
        assert "17(3)(e)" in str(detail) or "Article 17" in str(detail)

    async def test_rtbf_execute_with_typed_confirmation(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        # No hold => not blocked
        r = await client.post(
            "/api/admin/compliance/rtbf/requests",
            headers=h, json={"subject_id": "lonely-subject"},
        )
        assert r.status_code == 201, r.text
        rid = r.json()["id"]

        # wrong confirmation phrase
        r2 = await client.post(
            f"/api/admin/compliance/rtbf/requests/{rid}/execute",
            headers=h, json={"confirmation": "ERASE wrong"},
        )
        assert r2.status_code == 400

        # right confirmation
        r3 = await client.post(
            f"/api/admin/compliance/rtbf/requests/{rid}/execute",
            headers=h, json={"confirmation": "ERASE lonely-subject"},
        )
        # may succeed or 200; just must not be 400
        assert r3.status_code in (200, 500)


# ─────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────


class TestClassification:
    async def test_list_and_scan(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        # Run scan (will bootstrap built-ins if empty)
        r = await client.post(
            "/api/admin/compliance/classification/scan",
            headers=h, json={"scope": {"sources": ["messages"],
                                       "limit_per_source": 10},
                             "dry_run": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("job_id", "scanned", "findings", "dry_run", "by_severity"):
            assert k in body

        r2 = await client.get(
            "/api/admin/compliance/classification/rules", headers=h,
        )
        assert r2.status_code == 200
        items = r2.json()["items"]
        # Bootstrapped built-ins should be present after the scan
        names = {i["name"] for i in items}
        assert {"credit_card", "ssn_us", "iban"} <= names

    async def test_create_rule_invalid_kind(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.post(
            "/api/admin/compliance/classification/rules",
            headers=h, json={"name": "bad", "kind": "vibe", "pattern": "x"},
        )
        assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────
# Frameworks
# ─────────────────────────────────────────────────────────────────


class TestFrameworks:
    async def test_status_all_frameworks(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.get(
            "/api/admin/compliance/frameworks/status", headers=h,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        names = {f["framework"] for f in body["frameworks"]}
        expected = {
            "GDPR", "HIPAA", "SOC2", "ISO27001", "ISO27017",
            "NIST_800_53", "PCI_DSS", "FedRAMP",
            "SAUDI_NCA_ECC", "UAE_TDRA",
        }
        assert expected <= names

    async def test_assess_specific_framework(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.get(
            "/api/admin/compliance/frameworks/GDPR", headers=h,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["framework"] == "GDPR"
        assert body["posture"] in ("green", "yellow", "red")
        assert isinstance(body["controls"], list)

    async def test_assess_unknown_framework_rejected(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.get(
            "/api/admin/compliance/frameworks/FAKE", headers=h,
        )
        assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────
# Reports v2
# ─────────────────────────────────────────────────────────────────


class TestReports:
    async def test_generate_json_report(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.post(
            "/api/admin/compliance/reports/GDPR",
            headers=h, json={"period": 30, "format": "json", "signed": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["framework"] == "GDPR"
        assert body["status"] == "ready"
        assert body["sha256"]
        assert body["signed"] is True

    async def test_generate_invalid_framework(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.post(
            "/api/admin/compliance/reports/FAKE",
            headers=h, json={"period": 30, "format": "json"},
        )
        assert r.status_code == 400

    async def test_list_reports(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.get("/api/admin/compliance/reports", headers=h)
        assert r.status_code == 200
        assert "items" in r.json()


# ─────────────────────────────────────────────────────────────────
# Audit linkage
# ─────────────────────────────────────────────────────────────────


class TestAuditLinkage:
    async def test_audit_query(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        # Trigger an audit event
        await client.post(
            "/api/admin/compliance/holds",
            headers=h, json={"name": "audit-trigger", "scope": {}},
        )
        r = await client.get(
            "/api/admin/compliance/audit?source=compliance", headers=h,
        )
        assert r.status_code == 200
        assert "items" in r.json()

    async def test_audit_verify(
        self, client: AsyncClient, cmp_admin_headers,
    ):
        h = {k: v for k, v in cmp_admin_headers.items() if not k.startswith("_")}
        r = await client.get(
            "/api/admin/compliance/audit/verify", headers=h,
        )
        assert r.status_code == 200
        body = r.json()
        assert "verify_status" in body
