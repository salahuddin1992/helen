"""Compliance / eDiscovery Workbench tables — Module AB part B.

Adds:
    compliance_holds
    compliance_hold_audit
    compliance_retention_policies_v2
    compliance_retention_jobs
    compliance_cases
    compliance_case_evidence
    compliance_case_exports
    compliance_dsar_requests
    compliance_rtbf_requests
    compliance_classification_rules
    compliance_classification_findings
    compliance_reports_v2
    compliance_report_schedules

Revision ID: helen_compliance_workbench_addon
Revises: helen_compliance_addon
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "helen_compliance_workbench_addon"
# Re-pointed off helen_federation_health_map_addon (chain head at this
# point). Original down was helen_compliance_addon — that ancestor is
# still reachable via the depends_on directive.
down_revision = "helen_federation_health_map_addon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── holds ──────────────────────────────────────────────
    op.create_table(
        "compliance_holds",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("case_ref", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("retention_override", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("notify", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.String(length=64), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cmp_hold_status", "compliance_holds", ["status"])
    op.create_index("ix_cmp_hold_case_ref", "compliance_holds", ["case_ref"])
    op.create_index("ix_cmp_hold_created_by", "compliance_holds", ["created_by"])

    op.create_table(
        "compliance_hold_audit",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("hold_id", sa.String(length=32),
                  sa.ForeignKey("compliance_holds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("occurred_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("details", sa.JSON(), nullable=True),
    )
    op.create_index("ix_cmp_hold_audit_hold_id", "compliance_hold_audit", ["hold_id"])
    op.create_index("ix_cmp_hold_audit_occurred_at", "compliance_hold_audit", ["occurred_at"])

    # ── retention v2 ───────────────────────────────────────
    op.create_table(
        "compliance_retention_policies_v2",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("selector", sa.JSON(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("action", sa.String(length=16), nullable=False, server_default="delete"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("respect_legal_hold", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_affected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_run_skipped_held", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cmp_retv2_enabled", "compliance_retention_policies_v2", ["enabled"])
    op.create_index("ix_cmp_retv2_resource_type", "compliance_retention_policies_v2",
                    ["resource_type"])

    op.create_table(
        "compliance_retention_jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("policy_id", sa.String(length=32), nullable=True),
        sa.Column("actor_id", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("affected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_held", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("report", sa.JSON(), nullable=True),
    )
    op.create_index("ix_cmp_retjob_status", "compliance_retention_jobs", ["status"])
    op.create_index("ix_cmp_retjob_started_at", "compliance_retention_jobs", ["started_at"])

    # ── cases ──────────────────────────────────────────────
    op.create_table(
        "compliance_cases",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("matter_number", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("owner_id", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("custodians", sa.JSON(), nullable=False),
        sa.Column("hold_id", sa.String(length=32),
                  sa.ForeignKey("compliance_holds.id", ondelete="SET NULL"), nullable=True),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cmp_case_status", "compliance_cases", ["status"])
    op.create_index("ix_cmp_case_owner", "compliance_cases", ["owner_id"])

    op.create_table(
        "compliance_case_evidence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("case_id", sa.String(length=32),
                  sa.ForeignKey("compliance_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("tag", sa.String(length=32), nullable=False, server_default="relevant"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("added_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("added_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("case_id", "resource_type", "resource_id",
                            name="uq_cmp_case_evidence_resource"),
    )
    op.create_index("ix_cmp_case_evidence_case_id", "compliance_case_evidence", ["case_id"])
    op.create_index("ix_cmp_case_evidence_tag", "compliance_case_evidence", ["tag"])

    op.create_table(
        "compliance_case_exports",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("case_id", sa.String(length=32),
                  sa.ForeignKey("compliance_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("actor_id", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("signature", sa.String(length=512), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_cmp_case_export_case_id", "compliance_case_exports", ["case_id"])
    op.create_index("ix_cmp_case_export_status", "compliance_case_exports", ["status"])

    # ── DSAR ───────────────────────────────────────────────
    op.create_table(
        "compliance_dsar_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("subject_email", sa.String(length=256), nullable=True),
        sa.Column("subject_name", sa.String(length=256), nullable=True),
        sa.Column("request_type", sa.String(length=32), nullable=False, server_default="access"),
        sa.Column("identity_verified", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("identity_proof", sa.JSON(), nullable=True),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("received_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response_letter", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cmp_dsar_status", "compliance_dsar_requests", ["status"])
    op.create_index("ix_cmp_dsar_subject_id", "compliance_dsar_requests", ["subject_id"])
    op.create_index("ix_cmp_dsar_deadline", "compliance_dsar_requests", ["deadline_at"])

    # ── RTBF ───────────────────────────────────────────────
    op.create_table(
        "compliance_rtbf_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("subject_email", sa.String(length=256), nullable=True),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("hold_conflicts", sa.JSON(), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("messages_redacted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("audit_entries_marked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verification_report", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cmp_rtbf_status", "compliance_rtbf_requests", ["status"])
    op.create_index("ix_cmp_rtbf_subject_id", "compliance_rtbf_requests", ["subject_id"])

    # ── classification ─────────────────────────────────────
    op.create_table(
        "compliance_classification_rules",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False, server_default="tag"),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="medium"),
        sa.Column("classification", sa.String(length=32), nullable=False, server_default="pii"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cmp_clsrule_enabled", "compliance_classification_rules", ["enabled"])
    op.create_index("ix_cmp_clsrule_severity", "compliance_classification_rules", ["severity"])

    op.create_table(
        "compliance_classification_findings",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("rule_id", sa.String(length=32),
                  sa.ForeignKey("compliance_classification_rules.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("field", sa.String(length=128), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="medium"),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("matched_text", sa.String(length=512), nullable=True),
        sa.Column("found_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("extras", sa.JSON(), nullable=True),
    )
    op.create_index("ix_cmp_clsfind_resource", "compliance_classification_findings",
                    ["resource_type", "resource_id"])
    op.create_index("ix_cmp_clsfind_rule_id", "compliance_classification_findings", ["rule_id"])
    op.create_index("ix_cmp_clsfind_severity", "compliance_classification_findings", ["severity"])
    op.create_index("ix_cmp_clsfind_found_at", "compliance_classification_findings", ["found_at"])

    # ── reports v2 ─────────────────────────────────────────
    op.create_table(
        "compliance_reports_v2",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("framework", sa.String(length=32), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False, server_default="json"),
        sa.Column("period_start", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("period_end", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("signed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("signature", sa.String(length=512), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cmp_rep2_framework", "compliance_reports_v2", ["framework"])
    op.create_index("ix_cmp_rep2_status", "compliance_reports_v2", ["status"])
    op.create_index("ix_cmp_rep2_period_end", "compliance_reports_v2", ["period_end"])

    op.create_table(
        "compliance_report_schedules",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("framework", sa.String(length=32), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False, server_default="pdf"),
        sa.Column("cadence", sa.String(length=64), nullable=False, server_default="monthly"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_report_id", sa.String(length=32), nullable=True),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cmp_repsched_enabled", "compliance_report_schedules", ["enabled"])


def downgrade() -> None:
    for tbl in (
        "compliance_report_schedules",
        "compliance_reports_v2",
        "compliance_classification_findings",
        "compliance_classification_rules",
        "compliance_rtbf_requests",
        "compliance_dsar_requests",
        "compliance_case_exports",
        "compliance_case_evidence",
        "compliance_cases",
        "compliance_retention_jobs",
        "compliance_retention_policies_v2",
        "compliance_hold_audit",
        "compliance_holds",
    ):
        op.drop_table(tbl)
