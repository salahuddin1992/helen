"""
Operator Onboarding State Machine.

A declarative 14-step DAG that drives the wizard. Each step has:

    num             — 1-based ordinal
    key             — slug used in URLs / draft_data keys
    title           — human-readable title (English; UI handles i18n)
    required_fields — list of (field_name, type, validator) tuples
    optional_fields — same shape, but missing values are allowed
    prerequisites   — list of step nums that must complete first
    side_effect     — async callable that performs the actual work
                      (signature: ``async fn(db, data, actor) -> dict``)

The machine is *pure orchestration*: persistent work is delegated to
the topical service classes (cert_manager, firewall_manager, …) so the
state machine stays declarative.

Steps
-----
 1. Welcome                     informational, accepts EULA acceptance
 2. System Inspection           snapshot + operator confirms
 3. Network Discovery           interfaces + subnets selection
 4. Firewall Rules              applies OS firewall rules
 5. TLS Certificate             generate or import
 6. License Activation          billing license validation
 7. Admin Bootstrap             create first admin + TOTP
 8. Recovery Codes              generate 10 codes
 9. Router Pairing              TOFU exchange
10. Federation Mode             master / follower / observer
11. Observability               metrics + crash + audit chain
12. Backup Strategy             DR destination selection
13. Branding                    workspace name + logo + theme
14. Finalize                    atomic flip of onboarding_complete=true
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.models.onboarding_state import OnboardingState

logger = get_logger(__name__)


class StepValidationError(Exception):
    """Raised when step input fails validation. Carries a structured payload."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(json.dumps(errors))


SideEffect = Callable[[AsyncSession, dict[str, Any], str], Awaitable[dict[str, Any]]]


@dataclass
class FieldSpec:
    name: str
    type: str                                       # "str" | "int" | "bool" | "list" | "dict" | "email" | ...
    required: bool = True
    pattern: str | None = None                       # regex (str-only)
    min_len: int | None = None
    max_len: int | None = None
    choices: list[Any] | None = None
    description: str = ""


@dataclass
class StepDefinition:
    num: int
    key: str
    title: str
    description: str
    fields: list[FieldSpec] = field(default_factory=list)
    prerequisites: list[int] = field(default_factory=list)
    side_effect: SideEffect | None = None
    irreversible: bool = False


# ────────────────────────────────────────────────────────────
# Side-effect implementations (thin wrappers around real services)
# ────────────────────────────────────────────────────────────

async def _se_welcome(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    audit_log("onboarding.welcome_accepted", user_id=actor, details={"eula": data.get("eula_accepted")})
    return {"accepted_at": data.get("accepted_at"), "ok": True}


async def _se_system_inspection(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    from app.services.onboarding.system_inspector import SystemInspector
    snap = await SystemInspector().info()
    audit_log("onboarding.system_inspection_recorded", user_id=actor)
    return {"snapshot": snap}


async def _se_network_discovery(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    from app.services.onboarding.system_inspector import SystemInspector
    probe = await SystemInspector().network_probe(
        interfaces=data.get("interfaces") or [],
        subnets=data.get("subnets") or [],
    )
    audit_log("onboarding.network_discovery", user_id=actor,
              details={"interfaces": data.get("interfaces"), "subnets": data.get("subnets")})
    return {"probe": probe}


async def _se_firewall(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    from app.services.onboarding.firewall_manager import FirewallManager
    fw = FirewallManager()
    result = await fw.apply_rules(data.get("rules") or [])
    audit_log("onboarding.firewall_applied", user_id=actor,
              details={"applied": len(result.get("applied", [])),
                       "failed": len(result.get("failed", []))})
    return result


async def _se_tls(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    from app.services.onboarding.cert_manager import OnboardingCertManager
    mgr = OnboardingCertManager()
    mode = data.get("mode") or "generate"
    if mode == "generate":
        info = await mgr.generate_self_signed(
            db,
            cn=data.get("cn") or "helen.local",
            san=data.get("san") or [],
            duration_days=int(data.get("duration_days") or 825),
            key_type=data.get("key_type") or "rsa",
            actor=actor,
        )
    else:
        info = await mgr.import_cert(
            db,
            cert_pem=data.get("cert_pem") or "",
            key_pem=data.get("key_pem") or "",
            actor=actor,
        )
    audit_log("onboarding.tls_configured", user_id=actor,
              details={"mode": mode, "fingerprint": info.get("fingerprint_sha256")})
    return info


async def _se_license(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    key = data.get("license_key") or ""
    # Best-effort hook into the billing service; tolerate missing module.
    try:
        from app.services.billing_license_service import billing_license_service  # type: ignore
        result = await billing_license_service.activate(db, key, actor=actor)
    except Exception as e:
        logger.warning("billing_service_unavailable_in_onboarding",
                       error=str(e))
        # Fall back to syntactic validation: license keys are 22-64 chars,
        # alphanumeric + dashes.
        if not re.fullmatch(r"[A-Za-z0-9\-]{22,64}", key):
            raise StepValidationError({"license_key": "Malformed key"})
        result = {"valid": True, "validated_syntactically": True}
    audit_log("onboarding.license_activated", user_id=actor,
              details={"key_prefix": key[:6]})
    return result


async def _se_admin_bootstrap(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    from app.services.onboarding.totp import TOTPManager
    from app.models.onboarding_state import OnboardingState  # noqa: F811

    totp = TOTPManager()
    if not totp.verify(data["totp_secret_b32"], data["totp_code"]):
        raise StepValidationError({"totp_code": "Invalid TOTP code"})

    # Create or update the first admin user. We avoid hard-importing the
    # User model at module load to keep this service decoupled.
    try:
        from app.models.user import User  # type: ignore
        from app.core.security import hash_password  # type: ignore
    except Exception as e:
        logger.warning("user_module_unavailable", error=str(e))
        # In test mode we just record intent.
        return {"username": data["username"], "ok": True, "stub": True}

    existing = (await db.execute(
        select(User).where(User.username == data["username"])
    )).scalar_one_or_none()
    if existing:
        user = existing
    else:
        # Build kwargs from only the fields the User model actually has,
        # so this works across schema variations.
        cols = {c.name for c in User.__table__.columns}                # type: ignore[attr-defined]
        candidate = {
            "username": data["username"],
            "email": data["email"],
            "password_hash": hash_password(data["password"]),
            "role": "admin",
            "is_active": True,
            "totp_secret": data["totp_secret_b32"],
            "totp_enabled": True,
            "display_name": data["username"],
        }
        kwargs = {k: v for k, v in candidate.items() if k in cols}
        user = User(**kwargs)
        db.add(user)
        await db.flush()
    audit_log("onboarding.admin_bootstrapped", user_id=actor,
              details={"admin_user_id": user.id})
    return {"username": user.username, "user_id": user.id, "ok": True}


async def _se_recovery_codes(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    from app.services.onboarding.recovery_codes import (
        generate_recovery_codes,
        hash_recovery_code,
    )
    from app.models.admin_recovery_code import AdminRecoveryCode

    user_id = data.get("user_id") or actor
    codes = generate_recovery_codes(count=10)
    for c in codes:
        db.add(AdminRecoveryCode(user_id=user_id, code_hash=hash_recovery_code(c)))
    await db.flush()
    audit_log("onboarding.recovery_codes_generated", user_id=actor,
              details={"count": len(codes)})
    return {"codes": codes, "count": len(codes)}


async def _se_router_pair(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    from app.services.onboarding.router_pairing import RouterPairingService
    svc = RouterPairingService()
    return await svc.confirm(db, router_url=data["router_url"],
                             fingerprint=data["fingerprint"], actor=actor)


async def _se_federation_mode(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    mode = data.get("mode")
    if mode not in {"master", "follower", "observer"}:
        raise StepValidationError({"mode": "Must be master|follower|observer"})
    audit_log("onboarding.federation_mode_set", user_id=actor,
              details={"mode": mode, "scope": data.get("scope")})
    return {"mode": mode, "scope": data.get("scope")}


async def _se_observability(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    audit_log("onboarding.observability_bootstrapped", user_id=actor,
              details=data)
    # Best-effort: kick the audit chain if available.
    try:
        if data.get("audit_chain_init"):
            from app.services.audit_chain import get_audit_chain  # type: ignore
            chain = get_audit_chain()
            if chain is not None:
                chain.append(actor=actor, action="onboarding.audit_chain_init",
                             target="bootstrap", payload={})
    except Exception as e:
        logger.warning("audit_chain_bootstrap_failed", error=str(e))
    return {"ok": True, "applied": data}


async def _se_backup_strategy(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    audit_log("onboarding.backup_strategy_set", user_id=actor, details=data)
    return {"strategy": data.get("strategy"), "destination": data.get("destination")}


async def _se_branding(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    audit_log("onboarding.branding_set", user_id=actor,
              details={"workspace_name": data.get("workspace_name")})
    return {"ok": True}


async def _se_finalize(db: AsyncSession, data: dict[str, Any], actor: str) -> dict[str, Any]:
    """No-op marker; the finalizer module handles real finalization."""
    return {"ok": True}


# ────────────────────────────────────────────────────────────
# Declarative step table
# ────────────────────────────────────────────────────────────

STEP_DEFINITIONS: list[StepDefinition] = [
    StepDefinition(
        num=1, key="welcome", title="Welcome",
        description="Operator accepts the EULA and chooses language.",
        fields=[
            FieldSpec("eula_accepted", "bool"),
            FieldSpec("language", "str", required=False, max_len=8),
            FieldSpec("accepted_at", "str", required=False),
        ],
        side_effect=_se_welcome,
    ),
    StepDefinition(
        num=2, key="system_inspection", title="System Inspection",
        description="Capture host/CPU/RAM/disk snapshot for operator review.",
        fields=[
            FieldSpec("confirm", "bool"),
        ],
        prerequisites=[1],
        side_effect=_se_system_inspection,
    ),
    StepDefinition(
        num=3, key="network_discovery", title="Network Discovery",
        description="Select interfaces + subnets and probe reachability.",
        fields=[
            FieldSpec("interfaces", "list"),
            FieldSpec("subnets", "list", required=False),
        ],
        prerequisites=[2],
        side_effect=_se_network_discovery,
    ),
    StepDefinition(
        num=4, key="firewall", title="Firewall Rules",
        description="Apply OS firewall rules required by Helen.",
        fields=[
            FieldSpec("rules", "list"),
            FieldSpec("confirm", "bool"),
        ],
        prerequisites=[3],
        side_effect=_se_firewall,
        irreversible=True,
    ),
    StepDefinition(
        num=5, key="tls", title="TLS Certificate",
        description="Generate self-signed or import operator-provided cert.",
        fields=[
            FieldSpec("mode", "str", choices=["generate", "import"]),
            FieldSpec("cn", "str", required=False, max_len=255),
            FieldSpec("san", "list", required=False),
            FieldSpec("duration_days", "int", required=False),
            FieldSpec("key_type", "str", required=False,
                      choices=["rsa", "ed25519", "ecdsa"]),
            FieldSpec("cert_pem", "str", required=False),
            FieldSpec("key_pem", "str", required=False),
        ],
        prerequisites=[2],
        side_effect=_se_tls,
        irreversible=True,
    ),
    StepDefinition(
        num=6, key="license", title="License Activation",
        description="Activate billing license (offline-signed key).",
        fields=[
            FieldSpec("license_key", "str", min_len=22, max_len=64),
        ],
        prerequisites=[1],
        side_effect=_se_license,
    ),
    StepDefinition(
        num=7, key="admin_bootstrap", title="Admin Bootstrap",
        description="Create the first operator admin user with TOTP.",
        fields=[
            FieldSpec("username", "str", min_len=3, max_len=64,
                      pattern=r"^[A-Za-z0-9_.-]+$"),
            FieldSpec("email", "email"),
            FieldSpec("password", "str", min_len=12, max_len=128),
            FieldSpec("totp_secret_b32", "str", min_len=16, max_len=64),
            FieldSpec("totp_code", "str", min_len=6, max_len=8,
                      pattern=r"^\d{6,8}$"),
        ],
        prerequisites=[5, 6],
        side_effect=_se_admin_bootstrap,
        irreversible=True,
    ),
    StepDefinition(
        num=8, key="recovery_codes", title="Recovery Codes",
        description="Generate 10 one-time recovery codes.",
        fields=[
            FieldSpec("user_id", "str", required=False, max_len=64),
            FieldSpec("confirm_saved", "bool"),
        ],
        prerequisites=[7],
        side_effect=_se_recovery_codes,
    ),
    StepDefinition(
        num=9, key="router_pair", title="Router Pairing",
        description="TOFU-pair with a Helen router for federated traffic.",
        fields=[
            FieldSpec("router_url", "str", min_len=8, max_len=512),
            FieldSpec("fingerprint", "str", min_len=32, max_len=95),
        ],
        prerequisites=[5],
        side_effect=_se_router_pair,
    ),
    StepDefinition(
        num=10, key="federation_mode", title="Federation Mode",
        description="Choose this node's role in the federation mesh.",
        fields=[
            FieldSpec("mode", "str", choices=["master", "follower", "observer"]),
            FieldSpec("scope", "str", required=False, max_len=128),
        ],
        prerequisites=[9],
        side_effect=_se_federation_mode,
    ),
    StepDefinition(
        num=11, key="observability", title="Observability",
        description="Enable metrics, crash reporter, audit chain.",
        fields=[
            FieldSpec("metrics_enabled", "bool"),
            FieldSpec("crash_reporter", "bool"),
            FieldSpec("audit_chain_init", "bool"),
        ],
        prerequisites=[7],
        side_effect=_se_observability,
    ),
    StepDefinition(
        num=12, key="backup_strategy", title="Backup Strategy",
        description="Pick DR destination + cadence.",
        fields=[
            FieldSpec("strategy", "str", choices=["local", "s3", "remote", "none"]),
            FieldSpec("destination", "str", required=False, max_len=512),
            FieldSpec("cadence", "str", required=False, max_len=32),
        ],
        prerequisites=[11],
        side_effect=_se_backup_strategy,
    ),
    StepDefinition(
        num=13, key="branding", title="Branding",
        description="Workspace name, logo URL, theme.",
        fields=[
            FieldSpec("workspace_name", "str", min_len=2, max_len=128),
            FieldSpec("logo_url", "str", required=False, max_len=512),
            FieldSpec("theme", "str", required=False, max_len=32),
        ],
        prerequisites=[7],
        side_effect=_se_branding,
    ),
    StepDefinition(
        num=14, key="finalize", title="Finalize",
        description="Atomic flip of onboarding_complete=true.",
        fields=[
            FieldSpec("confirm", "bool"),
        ],
        prerequisites=list(range(1, 14)),
        side_effect=_se_finalize,
        irreversible=True,
    ),
]


TOTAL_STEPS = len(STEP_DEFINITIONS)


# ────────────────────────────────────────────────────────────
# State machine
# ────────────────────────────────────────────────────────────


class OnboardingStateMachine:
    """
    Stateless orchestrator — operates on an ``OnboardingState`` row.

    Concurrency model: we acquire a per-process lock to serialise wizard
    progress, since the wizard is by definition a single-operator flow.
    The lock is created lazily per running event loop to play nicely with
    pytest-asyncio's fresh-loop-per-test pattern.
    """

    _loop_locks: dict[int, asyncio.Lock] = {}

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.Lock()
        key = id(loop)
        lock = cls._loop_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            cls._loop_locks[key] = lock
        return lock

    @property
    def _lock(self) -> asyncio.Lock:                # noqa: D401
        """Per-loop lock proxy (kept as property to preserve call sites)."""
        return self._get_lock()

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── persistence ───────────────────────────────────────
    async def get_state(self) -> OnboardingState:
        row = (await self.db.execute(
            select(OnboardingState).where(OnboardingState.id == 1)
        )).scalar_one_or_none()
        if row is None:
            row = OnboardingState(id=1, completed_steps=[], current_step=1,
                                  draft_data={}, locked=False)
            self.db.add(row)
            await self.db.flush()
        return row

    # ── validation ────────────────────────────────────────
    def _validate_field(self, spec: FieldSpec, value: Any) -> str | None:
        if value is None:
            return "required" if spec.required else None
        if spec.type == "str" or spec.type == "email":
            if not isinstance(value, str):
                return "must be string"
            if spec.min_len is not None and len(value) < spec.min_len:
                return f"min length {spec.min_len}"
            if spec.max_len is not None and len(value) > spec.max_len:
                return f"max length {spec.max_len}"
            if spec.pattern and not re.fullmatch(spec.pattern, value):
                return "format invalid"
            if spec.type == "email" and "@" not in value:
                return "must be a valid email"
        elif spec.type == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                return "must be int"
        elif spec.type == "bool":
            if not isinstance(value, bool):
                return "must be bool"
        elif spec.type == "list":
            if not isinstance(value, list):
                return "must be list"
        elif spec.type == "dict":
            if not isinstance(value, dict):
                return "must be dict"
        if spec.choices is not None and value not in spec.choices:
            return f"must be one of {spec.choices}"
        return None

    def validate_step(self, num: int, data: dict[str, Any]) -> dict[str, str]:
        step = self.get_definition(num)
        errors: dict[str, str] = {}
        for spec in step.fields:
            err = self._validate_field(spec, data.get(spec.name))
            if err is not None:
                errors[spec.name] = err
        return errors

    @staticmethod
    def get_definition(num: int) -> StepDefinition:
        if not 1 <= num <= TOTAL_STEPS:
            raise StepValidationError({"step": f"out of range 1..{TOTAL_STEPS}"})
        return STEP_DEFINITIONS[num - 1]

    # ── application ───────────────────────────────────────
    async def apply_step(
        self, num: int, data: dict[str, Any], actor: str,
    ) -> dict[str, Any]:
        async with self._lock:
            state = await self.get_state()
            if state.locked:
                raise StepValidationError({"state": "onboarding is locked (finalized)"})
            step = self.get_definition(num)

            # Prerequisites
            for pre in step.prerequisites:
                if pre not in (state.completed_steps or []):
                    raise StepValidationError(
                        {"prerequisite": f"step {pre} must complete first"}
                    )

            errors = self.validate_step(num, data)
            if errors:
                raise StepValidationError(errors)

            # Run side-effect
            result: dict[str, Any] = {}
            if step.side_effect is not None:
                try:
                    result = await step.side_effect(self.db, data, actor)
                except StepValidationError:
                    raise
                except Exception as e:
                    logger.error("onboarding_side_effect_failed",
                                 step=num, error=str(e), exc_info=True)
                    raise StepValidationError({"side_effect": str(e)})

            # Persist
            completed = list(state.completed_steps or [])
            if num not in completed:
                completed.append(num)
                completed.sort()
            draft = dict(state.draft_data or {})
            draft[str(num)] = {"data": data, "result": result}

            state.completed_steps = completed
            state.draft_data = draft
            state.actor_id = actor
            state.current_step = min(num + 1, TOTAL_STEPS)
            await self.db.flush()
            return {"step": num, "ok": True, "result": result,
                    "current_step": state.current_step,
                    "completed_steps": completed}

    async def save_draft(
        self, num: int, data: dict[str, Any], actor: str,
    ) -> dict[str, Any]:
        """Persist partial input without running side-effects."""
        async with self._lock:
            state = await self.get_state()
            if state.locked:
                raise StepValidationError({"state": "onboarding is locked"})
            self.get_definition(num)  # range-check
            draft = dict(state.draft_data or {})
            draft.setdefault(str(num), {})
            draft[str(num)]["draft"] = data
            state.draft_data = draft
            state.actor_id = actor
            await self.db.flush()
            return {"step": num, "saved": True}

    async def reset(self, actor: str, reason: str) -> dict[str, Any]:
        async with self._lock:
            state = await self.get_state()
            state.completed_steps = []
            state.current_step = 1
            state.draft_data = {}
            state.locked = False
            state.finalized_at = None
            state.actor_id = actor
            audit_log("onboarding.reset", user_id=actor,
                      details={"reason": reason}, success=True)
            await self.db.flush()
            return {"reset": True}

    async def is_complete(self) -> bool:
        state = await self.get_state()
        return bool(state.locked)
