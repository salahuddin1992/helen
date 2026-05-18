"""
Phase 6 / Module AB — heuristic PII classifier.

Scans every ORM column registered on ``app.db.base.Base`` and tags it as
PII / PHI / financial / credentials / none using simple substring rules.
Persisted into ``compliance_pii_inventory`` on bootstrap or via the admin
``POST /api/admin/compliance/pii-inventory/rebuild`` endpoint.

This is intentionally rule-based — runtime is O(columns) and never reads
row data. Operators are expected to refine the inventory through the
admin UI after the initial pass.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.base import Base
from app.db.session import async_session_factory
from app.models.compliance import PIIInventoryEntry

logger = get_logger(__name__)


# Order matters: first match wins.
_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("phi",         ("medical_", "diagnosis", "icd_", "patient_", "phi")),
    ("financial",   ("iban", "bic", "credit_card", "card_number", "cvv",
                     "swift", "account_number", "tax_id", "vat_id",
                     "billing_address", "stripe_", "amount", "currency")),
    ("credentials", ("password", "secret", "token", "api_key", "private_key",
                     "refresh_token", "access_token", "session_key",
                     "encryption_key", "totp", "otp_seed")),
    ("pii",         ("email", "phone", "mobile", "address", "city", "country",
                     "zip", "postcode", "first_name", "last_name", "full_name",
                     "display_name", "ip_address", "user_agent", "national_id",
                     "passport", "ssn", "dob", "date_of_birth", "gender",
                     "lat", "lng", "longitude", "latitude")),
]


def classify(table: str, column: str) -> str:
    name = (column or "").lower()
    for klass, needles in _RULES:
        for n in needles:
            if n in name:
                return klass
    return "none"


def scan_models() -> List[Dict[str, str]]:
    """Walk the SQLAlchemy registry; produce one dict per column."""
    out: List[Dict[str, str]] = []
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        tname = table.name
        for col in table.columns:
            cname = col.name
            classification = classify(tname, cname)
            out.append({
                "table_name": tname,
                "column_name": cname,
                "classification": classification,
            })
    return out


async def rebuild_inventory() -> Dict[str, Any]:
    """Sync DB inventory with current model definitions."""
    fresh = scan_models()
    added = 0
    updated = 0
    async with async_session_factory() as db:
        existing = (await db.execute(select(PIIInventoryEntry))).scalars().all()
        by_key = {(e.table_name, e.column_name): e for e in existing}
        seen: set[tuple[str, str]] = set()
        for entry in fresh:
            key = (entry["table_name"], entry["column_name"])
            seen.add(key)
            row = by_key.get(key)
            if row is None:
                db.add(PIIInventoryEntry(
                    id=uuid.uuid4().hex,
                    table_name=entry["table_name"],
                    column_name=entry["column_name"],
                    classification=entry["classification"],
                    encryption_status="plain",
                ))
                added += 1
            elif row.classification != entry["classification"]:
                row.classification = entry["classification"]
                updated += 1
        await db.commit()
    return {"added": added, "updated": updated, "total": len(fresh)}


async def heatmap() -> Dict[str, Dict[str, int]]:
    """Return a table-by-classification count grid for the UI."""
    grid: Dict[str, Dict[str, int]] = {}
    async with async_session_factory() as db:
        rows = (await db.execute(select(PIIInventoryEntry))).scalars().all()
    for r in rows:
        bucket = grid.setdefault(r.table_name, {})
        bucket[r.classification] = bucket.get(r.classification, 0) + 1
    return grid
