"""
Auth REST endpoints — register, login, refresh, logout.

Hardened:
  - Login rate limiting (IP-based + account lockout)
  - Password strength validation on registration
  - Audit logging for all auth events
  - Sanitized error responses
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_account_locked, audit_login, audit_logout, audit_token_refresh
from app.core.config import get_settings
from app.core.crypto import validate_password_strength
from app.core.deps import get_current_user_id, get_db
from app.core.middleware import account_lockout, login_tracker
from app.schemas.auth import (
    AdminResetPasswordRequest,
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserBrief,
)
from app.services.auth_service import AuthService
from app.core.security import (
    hash_password, hash_password_async,
    verify_password, verify_password_async,
)
from app.models.user import User
from sqlalchemy import select

settings = get_settings()
router = APIRouter(prefix="/auth", tags=["auth"])


def _get_client_ip(request: Request) -> str:
    """Extract client IP, considering X-Forwarded-For if present."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(
    body: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = _get_client_ip(request)

    # Rate limit registration attempts by IP
    if not login_tracker.check(ip):
        audit_login(body.username, ip, success=False, reason="rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please wait before trying again.",
        )

    # Password strength validation
    is_valid, error_msg = validate_password_strength(body.password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # Username validation
    if not body.username.isalnum() and not all(c.isalnum() or c in "_-." for c in body.username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username can only contain letters, digits, underscores, hyphens, and dots",
        )

    user, access_token, refresh_token = await AuthService.register(
        db=db,
        username=body.username,
        display_name=body.display_name,
        password=body.password,
        avatar_url=body.avatar_url,
        bio=body.bio,
    )

    audit_login(user.id, ip, success=True, reason="registration")
    login_tracker.record_attempt(ip, success=True)

    return AuthResponse(
        user=UserBrief.model_validate(user),
        tokens=TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        ),
    )


@router.post("/login", response_model=AuthResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = _get_client_ip(request)

    # IP-based rate limiting
    if not login_tracker.check(ip):
        audit_login(body.username, ip, success=False, reason="ip_rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please wait before trying again.",
        )

    # Account lockout check
    if account_lockout.is_locked(body.username):
        audit_account_locked(body.username, ip, "account_lockout")
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Account temporarily locked due to too many failed attempts. Try again later.",
        )

    user = None
    access_token = None
    refresh_token = None
    try:
        user, access_token, refresh_token = await AuthService.login(
            db=db,
            username=body.username,
            password=body.password,
            device_name=body.device_name,
            ip_address=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        # Local credentials failed. Try LDAP if configured — corporate
        # users keep their AD password without re-registering. LDAP
        # path also auto-creates / updates the local user row so JWT
        # issuance and group → role mapping work end-to-end.
        try:
            ldap_outcome = await _try_ldap_login(
                db, body.username, body.password,
                device_name=body.device_name,
                ip=ip,
                user_agent=request.headers.get("user-agent"),
            )
        except Exception:
            ldap_outcome = None

        if ldap_outcome is None:
            login_tracker.record_attempt(ip, success=False)
            account_lockout.record_failure(body.username)
            audit_login(body.username, ip, success=False,
                        reason="invalid_credentials")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )

        user, access_token, refresh_token = ldap_outcome
        audit_login(user.id, ip, success=True, reason="ldap")

    # Success
    login_tracker.record_attempt(ip, success=True)
    account_lockout.record_success(body.username)
    audit_login(user.id, ip, success=True)

    return AuthResponse(
        user=UserBrief.model_validate(user),
        tokens=TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        ),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(
    body: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = _get_client_ip(request)
    try:
        access_token, refresh_token = await AuthService.refresh_tokens(
            db=db,
            refresh_token_str=body.refresh_token,
        )
        audit_token_refresh("unknown", ip, success=True)
    except Exception:
        audit_token_refresh("unknown", ip, success=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/change-password", status_code=204, response_class=Response)
async def change_password(
    body: ChangePasswordRequest,
    user_id: str = Depends(get_current_user_id),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Authenticated user changes their own password.

    The old password must be verified — never trust the JWT alone for a
    sensitive mutation like this; a stolen access token would otherwise
    let an attacker rotate the password and lock the real owner out.
    """
    ip = _get_client_ip(request) if request else ""

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if not (await verify_password_async(body.current_password, user.password_hash or "")):
        audit_login(user_id, ip, success=False, reason="change_password_wrong_current")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )

    # Reuse the registration strength rules — same minimum bar everywhere.
    ok, reason = validate_password_strength(body.new_password)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)

    user.password_hash = await hash_password_async(body.new_password)
    await db.commit()

    audit_login(user_id, ip, success=True, reason="password_changed")
    return Response(status_code=204)


@router.post("/logout", status_code=204, response_class=Response)
async def logout(
    body: RefreshRequest | None = None,
    user_id: str = Depends(get_current_user_id),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    ip = _get_client_ip(request) if request else ""
    await AuthService.logout(
        db=db,
        user_id=user_id,
        refresh_token_str=body.refresh_token if body else None,
    )
    audit_logout(user_id, ip)
    return Response(status_code=204)


# ────────────────────────────────────────────────────────────────
# LDAP / Active Directory fallback for /login
# ────────────────────────────────────────────────────────────────


async def _try_ldap_login(
    db: AsyncSession,
    username: str,
    password: str,
    *,
    device_name: str | None,
    ip: str | None,
    user_agent: str | None,
) -> tuple[User, str, str] | None:
    """Authenticate against the configured directory; if successful,
    upsert a local mirror user and issue Helen JWT tokens.

    Returns ``(user, access, refresh)`` or ``None`` if LDAP is not
    configured or credentials don't validate. Never raises — caller
    treats every failure as "wrong password".
    """
    from datetime import datetime, timedelta, timezone
    import asyncio
    import secrets as _secrets

    try:
        from app.services.ldap_auth import (
            LDAPConfig,
            LDAPAuthenticator,
            LDAPAuthError,
        )
    except Exception:
        return None

    cfg = LDAPConfig.from_env()
    if cfg is None:
        return None

    # ldap3 is sync — run the bind in a worker thread so we don't
    # block the event loop on a slow directory.
    def _do_auth():
        return LDAPAuthenticator(cfg).authenticate(username, password)

    try:
        profile = await asyncio.to_thread(_do_auth)
    except LDAPAuthError:
        return None
    except Exception:
        return None

    # Upsert a local mirror user. We use a random-looking placeholder
    # password hash so brute-forcing the local table can never match
    # the LDAP user — they MUST go through LDAP every login.
    from app.core.security import (
        create_access_token, create_refresh_token, hash_password,
    )
    from app.core.crypto import hash_refresh_token
    from app.core.share_code import generate_share_code
    from app.models.session import UserSession

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        # Use an unrecoverable placeholder so local /login can't accept it.
        placeholder = hash_password(_secrets.token_urlsafe(32))
        user = User(
            username=username,
            share_code=generate_share_code(),
            display_name=profile.display_name or username,
            password_hash=placeholder,
            avatar_url=None,
            bio=None,
            status="online",
            role=profile.helen_role or "user",
        )
        db.add(user)
        await db.flush()
    else:
        # Refresh role + display name from LDAP each successful login.
        if profile.display_name:
            user.display_name = profile.display_name
        if profile.helen_role and user.role != profile.helen_role:
            user.role = profile.helen_role
        user.status = "online"
        user.last_seen = datetime.now(timezone.utc)

    access = create_access_token(user.id, role=user.role)
    refresh = create_refresh_token(user.id)

    session = UserSession(
        user_id=user.id,
        token_hash=hash_refresh_token(refresh),
        device_name=device_name,
        ip_address=ip,
        user_agent=user_agent,
        expires_at=(
            datetime.now(timezone.utc)
            + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
        ),
    )
    db.add(session)
    await db.commit()
    await db.refresh(user)
    return user, access, refresh
