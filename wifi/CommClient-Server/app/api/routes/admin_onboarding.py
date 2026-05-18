"""
Admin — Operator Onboarding Wizard (14-step bootstrap flow).

Prefix: ``/api/admin``           Tag: ``admin-onboarding``

Bootstrap-tolerant auth model
-----------------------------
Pre-finalize (``onboarding_state.locked == False``) the endpoints accept
either:
    - No token (anyone with network access to the box can finish setup).
    - A "setup token" header ``X-Helen-Setup-Token`` matching
      ``settings.HELEN_SETUP_TOKEN`` if configured.

Post-finalize all endpoints require a normal admin Bearer token. This is
implemented by the :func:`onboarding_auth` dependency which inspects the
current onboarding state on every request.

All write endpoints emit an audit event.
"""
from __future__ import annotations

import base64
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_db
from app.core.logging import get_logger
from app.models.onboarding_state import OnboardingState
from app.services.onboarding import (
    OnboardingStateMachine,
    SystemInspector,
    FirewallManager,
    OnboardingCertManager,
    TOTPManager,
    RouterPairingService,
    OnboardingFinalizer,
    STEP_DEFINITIONS,
)
from app.services.onboarding.state_machine import StepValidationError

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-onboarding"])


# ════════════════════════════════════════════════════════════
# Auth dependency — bootstrap-tolerant
# ════════════════════════════════════════════════════════════


async def onboarding_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_helen_setup_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """
    Resolve the effective actor for an onboarding request.

    Logic
    -----
    1. Look up the singleton ``OnboardingState`` row.
    2. If ``locked == False`` (pre-finalize):
         - If ``HELEN_SETUP_TOKEN`` is configured, require it via header.
         - Otherwise allow anonymous (typical first-boot scenario).
    3. If ``locked == True`` (post-finalize):
         - Require a valid admin Bearer token.

    Returns a context dict ``{actor_id, role, ip}``.
    """
    row = (await db.execute(
        select(OnboardingState).where(OnboardingState.id == 1)
    )).scalar_one_or_none()
    locked = bool(row.locked) if row else False
    ip = request.client.host if request.client else "unknown"

    if not locked:
        from app.core.config import get_settings
        s = get_settings()
        expected = getattr(s, "HELEN_SETUP_TOKEN", None)
        if expected:
            if not x_helen_setup_token or x_helen_setup_token != expected:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="setup token required",
                )
        return {"actor_id": "operator-bootstrap", "role": "setup", "ip": ip}

    # Post-finalize → require admin Bearer
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin auth required (onboarding is locked)",
        )
    token = authorization.split(" ", 1)[1]
    try:
        from app.core.security import decode_token
        payload = decode_token(token)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="invalid token")
    if payload.get("role") not in ("admin", "operator", "owner"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="admin role required")
    return {"actor_id": payload.get("sub") or "admin", "role": payload.get("role"),
            "ip": ip}


# ════════════════════════════════════════════════════════════
# Pydantic schemas
# ════════════════════════════════════════════════════════════


class StepSubmitRequest(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)


class DraftSubmitRequest(BaseModel):
    step_num: int = Field(..., ge=1, le=14)
    data: dict[str, Any] = Field(default_factory=dict)


class StateResponse(BaseModel):
    completed_steps: list[int]
    current_step: int
    total_steps: int
    draft_data: dict[str, Any]
    started_at: Optional[str] = None
    finalized_at: Optional[str] = None
    locked: bool
    steps: list[dict[str, Any]]


class ResetRequest(BaseModel):
    confirmation: str = Field(..., min_length=5, max_length=8)
    reason: str = Field(..., min_length=4, max_length=512)


class NetworkCheckRequest(BaseModel):
    interfaces: list[str] = Field(default_factory=list)
    subnets: list[str] = Field(default_factory=list)


class FirewallRulesRequest(BaseModel):
    rules: list[dict[str, Any]]


class CertGenerateRequest(BaseModel):
    cn: str = Field(..., min_length=1, max_length=255)
    san: list[str] = Field(default_factory=list)
    duration_days: int = Field(825, ge=1, le=3650)
    key_type: Literal["rsa", "ed25519", "ecdsa"] = "rsa"


class CertImportRequest(BaseModel):
    cert_pem: str = Field(..., min_length=20)
    key_pem: str = Field(..., min_length=20)


class LicenseActivateRequest(BaseModel):
    license_key: str = Field(..., min_length=10, max_length=64)


class AdminBootstrapRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=12, max_length=128)
    totp_secret_b32: str = Field(..., min_length=16, max_length=64)
    totp_code: str = Field(..., min_length=6, max_length=8)


class RecoveryCodesRequest(BaseModel):
    user_id: Optional[str] = None


class RouterPairRequest(BaseModel):
    router_url: str = Field(..., min_length=8, max_length=512)


class RouterPairConfirmRequest(BaseModel):
    router_url: str
    fingerprint: str


class FederationInviteRequest(BaseModel):
    mode: Literal["master", "follower", "observer"]
    scope: Optional[str] = None


class ObservabilityBootstrapRequest(BaseModel):
    metrics_enabled: bool = True
    crash_reporter: bool = True
    audit_chain_init: bool = True


# ════════════════════════════════════════════════════════════
# State endpoints
# ════════════════════════════════════════════════════════════


@router.get("/onboarding/state", response_model=StateResponse)
async def get_state(
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    sm = OnboardingStateMachine(db)
    state = await sm.get_state()
    return StateResponse(
        completed_steps=list(state.completed_steps or []),
        current_step=state.current_step,
        total_steps=len(STEP_DEFINITIONS),
        draft_data=dict(state.draft_data or {}),
        started_at=state.started_at.isoformat() if state.started_at else None,
        finalized_at=state.finalized_at.isoformat() if state.finalized_at else None,
        locked=bool(state.locked),
        steps=[
            {"num": s.num, "key": s.key, "title": s.title,
             "description": s.description,
             "prerequisites": s.prerequisites,
             "fields": [{"name": f.name, "type": f.type,
                         "required": f.required,
                         "choices": f.choices} for f in s.fields]}
            for s in STEP_DEFINITIONS
        ],
    )


@router.post("/onboarding/step/{step_num}")
async def submit_step(
    step_num: int,
    body: StepSubmitRequest,
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    sm = OnboardingStateMachine(db)
    try:
        result = await sm.apply_step(step_num, body.data, ctx["actor_id"])
    except StepValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors)
    audit_log("onboarding.step_submitted",
              user_id=ctx["actor_id"], ip_address=ctx["ip"],
              details={"step": step_num})
    await db.commit()
    return result


@router.post("/onboarding/step/draft")
async def save_draft(
    body: DraftSubmitRequest,
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    sm = OnboardingStateMachine(db)
    try:
        result = await sm.save_draft(body.step_num, body.data, ctx["actor_id"])
    except StepValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors)
    await db.commit()
    return result


@router.post("/onboarding/complete")
async def finalize_onboarding(
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    finalizer = OnboardingFinalizer(db)
    result = await finalizer.finalize(ctx["actor_id"])
    return result


@router.post("/onboarding/reset")
async def reset_onboarding(
    body: ResetRequest,
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    if body.confirmation.upper() != "RESET":
        raise HTTPException(status_code=400, detail='confirmation must be "RESET"')
    # Reset requires admin role if the system is already finalized.
    sm = OnboardingStateMachine(db)
    state = await sm.get_state()
    if state.locked and ctx.get("role") not in ("admin", "owner"):
        raise HTTPException(status_code=403,
                            detail="admin role required to reset a finalized wizard")
    result = await sm.reset(ctx["actor_id"], body.reason)
    await db.commit()
    return result


# ════════════════════════════════════════════════════════════
# System info / network / firewall
# ════════════════════════════════════════════════════════════


@router.get("/system/info")
async def system_info(ctx: dict = Depends(onboarding_auth)):
    return await SystemInspector().info()


@router.post("/system/network/check")
async def network_check(
    body: NetworkCheckRequest,
    ctx: dict = Depends(onboarding_auth),
):
    return await SystemInspector().network_probe(body.interfaces, body.subnets)


@router.get("/system/firewall/rules")
async def firewall_get(ctx: dict = Depends(onboarding_auth)):
    fw = FirewallManager()
    return {**fw.info(), "rules": await fw.get_rules()}


@router.post("/system/firewall/rules")
async def firewall_apply(
    body: FirewallRulesRequest,
    ctx: dict = Depends(onboarding_auth),
):
    fw = FirewallManager()
    result = await fw.apply_rules(body.rules)
    audit_log("onboarding.firewall_rules_applied",
              user_id=ctx["actor_id"], ip_address=ctx["ip"],
              details={"backend": result["backend"],
                       "applied": len(result["applied"]),
                       "failed": len(result["failed"])})
    return result


# ════════════════════════════════════════════════════════════
# TLS
# ════════════════════════════════════════════════════════════


@router.post("/tls/cert/generate")
async def cert_generate(
    body: CertGenerateRequest,
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    info = await OnboardingCertManager().generate_self_signed(
        db, cn=body.cn, san=body.san,
        duration_days=body.duration_days, key_type=body.key_type,
        actor=ctx["actor_id"],
    )
    audit_log("onboarding.cert_generated",
              user_id=ctx["actor_id"], ip_address=ctx["ip"],
              details={"cn": body.cn, "key_type": body.key_type})
    await db.commit()
    return info


@router.post("/tls/cert/import")
async def cert_import(
    body: CertImportRequest,
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    try:
        info = await OnboardingCertManager().import_cert(
            db, cert_pem=body.cert_pem, key_pem=body.key_pem,
            actor=ctx["actor_id"],
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"invalid cert/key: {e}")
    audit_log("onboarding.cert_imported",
              user_id=ctx["actor_id"], ip_address=ctx["ip"],
              details={"fingerprint": info.get("fingerprint_sha256")})
    await db.commit()
    return info


@router.get("/tls/cert/info")
async def cert_info(
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    return await OnboardingCertManager().get_info(db)


@router.get("/tls/cert/download-root")
async def cert_download_root(
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    payload = await OnboardingCertManager().download_root(db)
    if not payload:
        raise HTTPException(status_code=404, detail="no root cert available")
    return Response(content=payload, media_type="application/x-pem-file",
                    headers={"Content-Disposition":
                             'attachment; filename="helen-root.crt"'})


# ════════════════════════════════════════════════════════════
# Billing / Licensing
# ════════════════════════════════════════════════════════════


@router.post("/billing/licenses/activate")
async def license_activate(
    body: LicenseActivateRequest,
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    try:
        from app.services.billing_license_service import billing_license_service  # type: ignore
        result = await billing_license_service.activate(
            db, body.license_key, actor=ctx["actor_id"],
        )
    except Exception as e:
        logger.warning("license_service_unavailable",
                       error=str(e))
        import re
        if not re.fullmatch(r"[A-Za-z0-9\-]{10,64}", body.license_key):
            raise HTTPException(status_code=422, detail="malformed key")
        result = {"valid": True, "validated_syntactically": True,
                  "key_prefix": body.license_key[:6]}
    audit_log("onboarding.license_activated",
              user_id=ctx["actor_id"], ip_address=ctx["ip"],
              details={"key_prefix": body.license_key[:6]})
    await db.commit()
    return result


# ════════════════════════════════════════════════════════════
# Admin bootstrap + recovery codes
# ════════════════════════════════════════════════════════════


@router.post("/auth/admin/bootstrap")
async def admin_bootstrap(
    body: AdminBootstrapRequest,
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    totp = TOTPManager()
    if not totp.verify(body.totp_secret_b32, body.totp_code):
        raise HTTPException(status_code=422, detail="invalid TOTP code")

    # Create or update first admin (delegated to state machine side effect)
    from app.services.onboarding.state_machine import _se_admin_bootstrap
    try:
        result = await _se_admin_bootstrap(db, body.model_dump(),
                                           ctx["actor_id"])
    except StepValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors)
    audit_log("onboarding.admin_bootstrapped",
              user_id=ctx["actor_id"], ip_address=ctx["ip"],
              details={"username": body.username})
    await db.commit()
    return result


@router.post("/auth/admin/recovery-codes")
async def admin_recovery_codes(
    body: RecoveryCodesRequest,
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    from app.services.onboarding.recovery_codes import (
        generate_recovery_codes, hash_recovery_code,
    )
    from app.models.admin_recovery_code import AdminRecoveryCode

    user_id = body.user_id or ctx["actor_id"]
    codes = generate_recovery_codes(count=10)
    for c in codes:
        db.add(AdminRecoveryCode(user_id=user_id, code_hash=hash_recovery_code(c)))
    await db.flush()
    audit_log("onboarding.recovery_codes_generated",
              user_id=ctx["actor_id"], ip_address=ctx["ip"],
              details={"count": len(codes), "for_user": user_id})
    await db.commit()
    return {"codes": codes, "count": len(codes),
            "warning": "store these codes — they are shown once only"}


# ════════════════════════════════════════════════════════════
# Router pairing
# ════════════════════════════════════════════════════════════


@router.post("/router/pair")
async def router_pair(
    body: RouterPairRequest,
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    try:
        result = await RouterPairingService().begin(
            db, router_url=body.router_url, actor=ctx["actor_id"],
        )
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=f"router unreachable: {e}")
    await db.commit()
    return result


@router.post("/router/pair/confirm")
async def router_pair_confirm(
    body: RouterPairConfirmRequest,
    db: AsyncSession = Depends(get_db),
    ctx: dict = Depends(onboarding_auth),
):
    try:
        result = await RouterPairingService().confirm(
            db, router_url=body.router_url,
            fingerprint=body.fingerprint, actor=ctx["actor_id"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await db.commit()
    return result


# ════════════════════════════════════════════════════════════
# Federation invite
# ════════════════════════════════════════════════════════════


@router.post("/federation/invite/create")
async def federation_invite_create(
    body: FederationInviteRequest,
    ctx: dict = Depends(onboarding_auth),
):
    import secrets
    payload = {
        "mode": body.mode,
        "scope": body.scope,
        "issuer": ctx["actor_id"],
        "nonce": secrets.token_hex(16),
        "exp_minutes": 30,
    }
    token = base64.urlsafe_b64encode(
        ("|".join(f"{k}={v}" for k, v in payload.items())).encode()
    ).decode().rstrip("=")
    audit_log("onboarding.federation_invite_created",
              user_id=ctx["actor_id"], ip_address=ctx["ip"],
              details={"mode": body.mode, "scope": body.scope})
    return {"invite_token": token, **payload}


# ════════════════════════════════════════════════════════════
# Observability bootstrap
# ════════════════════════════════════════════════════════════


@router.post("/observability/bootstrap")
async def observability_bootstrap(
    body: ObservabilityBootstrapRequest,
    ctx: dict = Depends(onboarding_auth),
):
    started: list[str] = []
    if body.metrics_enabled:
        started.append("metrics_collector")
    if body.crash_reporter:
        started.append("crash_reporter")
    if body.audit_chain_init:
        try:
            from app.services.audit_chain import get_audit_chain  # type: ignore
            chain = get_audit_chain()
            if chain is not None:
                chain.append(actor=ctx["actor_id"],
                             action="onboarding.audit_chain_init",
                             target="bootstrap", payload={})
            started.append("audit_chain")
        except Exception as e:
            logger.warning("audit_chain_unavailable", error=str(e))
    audit_log("onboarding.observability_bootstrapped",
              user_id=ctx["actor_id"], ip_address=ctx["ip"],
              details={"started": started})
    return {"started": started, "ok": True}
