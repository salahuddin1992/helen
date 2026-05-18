"""
SIEM / Audit Chain — Production-grade subsystem for Helen Server.

This package layers advanced SIEM features on top of the existing
``app.services.audit_chain.AuditChain`` without modifying or breaking it.

Sub-modules
-----------
chain         — pub-sub wrapper around the legacy chain singleton.
                Re-exports the chain singleton and adds subscribe(callback).
audit_search  — re-exports the legacy search facility.
alert_rules   — DSL-driven alert rules engine + storage.
export_engine — async export jobs (jsonl-signed / csv / pdf / zip-verifier).
legal_hold    — legal-hold CRUD with conflict detection.
retention     — retention policy CRUD + preview + apply.
ws_stream     — live WebSocket fan-out for new audit entries + alerts.
"""

from __future__ import annotations

from app.services.audit import chain  # noqa: F401  side-effect: hooks pub-sub

__all__ = [
    "chain",
]
