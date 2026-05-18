"""
Zero-Trust — policy decision engine.

Pure-Python evaluator. No OPA / rego dependency. Policies are stored
in ``zt_access_policies``; each policy has a ``subject_selector`` and
``resource_selector`` (both dict matchers), an action string, and a
list of ``conditions`` evaluated against the request context.

Selector grammar
----------------
A selector is a dict where each key matches against the corresponding
field in the request:

    { "identity":  "spiffe://helen/user/*",
      "role":      ["admin", "operator"],
      "kind":      "user",
      "workspace": "ws_*" }

Globs use ``fnmatch`` semantics. Lists mean "any-of". Missing key in
the selector means "wildcard".

Conditions
----------
Available operators:
    - ``risk_score_lt``       : context.risk_score < N
    - ``device_attested``     : truthy
    - ``ip_in``               : context.ip in [...]
    - ``time_window``         : current hour ∈ [start, end]
    - ``mfa``                 : context.mfa_passed
    - ``geo_in``              : context.country in [...]

Obligations
-----------
Returned to the caller. Common values: ``require_mfa``,
``log_audit``, ``redact``, ``downgrade_session``.
"""
from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.zt import AccessPolicy, AccessRequest

logger = get_logger(__name__)


@dataclass
class DecisionContext:
    identity: str = ""
    workload_kind: str = "user"
    role: str = ""
    workspace: Optional[str] = None
    ip: str = ""
    country: str = ""
    risk_score: int = 0
    device_attested: bool = False
    mfa_passed: bool = False
    session_id: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyDecision:
    allow: bool
    reasons: list[str]
    obligations: list[str]
    matched_policy: Optional[str] = None


class PolicyEngine:
    """Stateless evaluator (DB-backed policies)."""

    _cached: Optional[list[AccessPolicy]] = None
    _cached_at: float = 0.0
    _cache_ttl: float = 30.0

    async def _load_policies(
        self, db: AsyncSession,
    ) -> list[AccessPolicy]:
        now = time.monotonic()
        if self._cached is not None and (now - self._cached_at) < self._cache_ttl:
            return self._cached
        rows = (await db.execute(
            select(AccessPolicy)
            .where(AccessPolicy.enabled == True)  # noqa: E712
            .order_by(asc(AccessPolicy.priority))
        )).scalars().all()
        self._cached = list(rows)
        self._cached_at = now
        return self._cached

    def invalidate_cache(self) -> None:
        self._cached = None

    async def evaluate(
        self,
        *,
        ctx: DecisionContext,
        resource: str,
        action: str,
        db: Optional[AsyncSession] = None,
        persist: bool = True,
    ) -> PolicyDecision:
        async def _do(db: AsyncSession) -> PolicyDecision:
            policies = await self._load_policies(db)
            for p in policies:
                if not self._subject_matches(p.subject_selector or {}, ctx):
                    continue
                if not self._resource_matches(p.resource_selector or {}, resource):
                    continue
                if p.action not in ("*", action):
                    continue
                cond_ok, cond_reasons = self._eval_conditions(
                    p.conditions or {}, ctx,
                )
                if not cond_ok:
                    if p.allow:
                        # An allow-policy with failed conditions falls
                        # through to the next rule.
                        continue
                    # A deny-policy with failed conditions still denies.
                    return PolicyDecision(
                        allow=False,
                        reasons=["policy:" + p.name] + cond_reasons,
                        obligations=list((p.obligations or {}).get("on_deny", [])),
                        matched_policy=p.id,
                    )
                obligations = list((p.obligations or {}).get("on_allow", [])) \
                    if p.allow else list((p.obligations or {}).get("on_deny", []))
                return PolicyDecision(
                    allow=p.allow,
                    reasons=["policy:" + p.name],
                    obligations=obligations,
                    matched_policy=p.id,
                )
            # Default deny.
            return PolicyDecision(
                allow=False, reasons=["no_matching_policy"], obligations=[],
            )

        if db is None:
            async with async_session_factory() as _db:
                decision = await _do(_db)
        else:
            decision = await _do(db)

        if persist:
            try:
                async with async_session_factory() as _db:
                    req = AccessRequest(
                        requester_identity=ctx.identity,
                        resource=resource,
                        action=action,
                        decision="allow" if decision.allow else "deny",
                        reasons=list(decision.reasons),
                        obligations=list(decision.obligations),
                        session_id=ctx.session_id,
                        risk_score=ctx.risk_score,
                    )
                    _db.add(req)
                    await _db.commit()
            except Exception as exc:
                logger.debug("zt_decision_persist_failed err=%s", exc)
        return decision

    # ── matchers ────────────────────────────────────────────

    def _subject_matches(self, sel: dict[str, Any], ctx: DecisionContext) -> bool:
        for key, val in sel.items():
            ctx_val = getattr(ctx, key, None)
            if ctx_val is None:
                ctx_val = ctx.extras.get(key)
            if not self._match(val, ctx_val):
                return False
        return True

    def _resource_matches(self, sel: dict[str, Any], resource: str) -> bool:
        if not sel:
            return True
        pattern = sel.get("pattern") or sel.get("uri") or "*"
        return fnmatch.fnmatchcase(str(resource), str(pattern))

    def _match(self, sel_val: Any, ctx_val: Any) -> bool:
        if isinstance(sel_val, list):
            return any(self._match(v, ctx_val) for v in sel_val)
        if isinstance(sel_val, str) and isinstance(ctx_val, str):
            return fnmatch.fnmatchcase(ctx_val, sel_val)
        return sel_val == ctx_val

    def _eval_conditions(
        self, conds: dict[str, Any], ctx: DecisionContext,
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        # risk
        rs_max = conds.get("risk_score_lt")
        if rs_max is not None and ctx.risk_score >= int(rs_max):
            reasons.append(f"risk:{ctx.risk_score}>={rs_max}")
            return False, reasons
        # device attested
        if conds.get("device_attested") and not ctx.device_attested:
            reasons.append("device:not_attested")
            return False, reasons
        # ip
        ip_list = conds.get("ip_in")
        if ip_list and ctx.ip not in ip_list:
            reasons.append(f"ip:{ctx.ip}_not_in_allowlist")
            return False, reasons
        # geo
        geo_list = conds.get("geo_in")
        if geo_list and ctx.country not in geo_list:
            reasons.append(f"geo:{ctx.country}_not_allowed")
            return False, reasons
        # time
        tw = conds.get("time_window")
        if tw and isinstance(tw, dict):
            hour = datetime.now(timezone.utc).hour
            start = int(tw.get("start") or 0)
            end = int(tw.get("end") or 23)
            if not (start <= hour <= end):
                reasons.append(f"time:hour_{hour}_outside_window")
                return False, reasons
        # mfa
        if conds.get("mfa") and not ctx.mfa_passed:
            reasons.append("mfa:not_passed")
            return False, reasons
        return True, reasons


_engine: Optional[PolicyEngine] = None


def get_policy_engine() -> PolicyEngine:
    global _engine
    if _engine is None:
        _engine = PolicyEngine()
    return _engine
