"""
Phase 3 / Module N — OAuth2 / OIDC authorization flow.

High-level entry points:
    start_authorization(provider, redirect_uri, desktop=False)
        -> dict(authorize_url, state, code_verifier)
    handle_callback(provider, code, state)
        -> tuple(User, is_new_user)

PKCE (RFC 7636) is always used for the desktop path and supported for
the browser path. State is persisted in ``oauth_states`` so a multi-worker
deployment can complete the round-trip on any node.

SAML 2.0 wrapper is a stub that returns a structured "not configured"
response unless ``python3-saml`` is importable.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.logging import get_logger
from app.core.security import create_access_token, create_refresh_token
from app.db.base import utc_now
from app.models.oauth import OAuthAccount, OAuthState
from app.models.user import User
from app.services.oauth.providers import (
    BaseOAuthProvider,
    get_provider,
    list_providers,
)

logger = get_logger(__name__)


# ── PKCE helpers ────────────────────────────────────────────

def _gen_state() -> str:
    return secrets.token_urlsafe(32)


def _gen_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ── Authorization start ─────────────────────────────────────

async def start_authorization(
    db: AsyncSession,
    provider_name: str,
    redirect_uri: str,
    *,
    desktop: bool = False,
    extra_scopes: Optional[list[str]] = None,
) -> dict[str, Any]:
    provider = get_provider(provider_name)
    if not provider:
        raise ValueError(
            f"provider '{provider_name}' is not configured. "
            f"Available: {list_providers() or '(none)'}"
        )

    state = _gen_state()
    code_verifier: Optional[str] = None
    code_challenge_str: Optional[str] = None
    if desktop:
        code_verifier = _gen_code_verifier()
        code_challenge_str = _code_challenge(code_verifier)

    row = OAuthState(
        state=state,
        code_verifier=code_verifier,
        provider=provider_name.lower(),
        redirect_uri=redirect_uri,
        desktop="1" if desktop else "0",
    )
    db.add(row)
    await db.flush()

    scopes = list(provider.scopes)
    if extra_scopes:
        for s in extra_scopes:
            if s not in scopes:
                scopes.append(s)

    params = {
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    if code_challenge_str:
        params["code_challenge"] = code_challenge_str
        params["code_challenge_method"] = "S256"
    params.update(provider.extra_params)

    return {
        "authorize_url": (
            provider.authorize_url + "?" + urllib.parse.urlencode(params)
        ),
        "state": state,
        "code_verifier": code_verifier,
    }


# ── Callback handling ───────────────────────────────────────

async def _exchange_code(
    provider: BaseOAuthProvider,
    code: str,
    redirect_uri: str,
    code_verifier: Optional[str],
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": provider.client_id,
    }
    if provider.client_secret:
        data["client_secret"] = provider.client_secret
    if code_verifier:
        data["code_verifier"] = code_verifier

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            provider.token_url,
            data=data,
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            # GitHub may respond as application/x-www-form-urlencoded
            return dict(urllib.parse.parse_qsl(r.text))


async def _fetch_userinfo(
    provider: BaseOAuthProvider, access_token: str,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            provider.userinfo_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        r.raise_for_status()
        info = r.json()

        # GitHub: /user lacks the email if it's private; fetch /user/emails
        if provider.name == "github" and not info.get("email"):
            try:
                r2 = await client.get(
                    "https://api.github.com/user/emails",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
                if r2.status_code == 200:
                    emails = r2.json()
                    primary = next(
                        (e for e in emails if e.get("primary") and e.get("verified")),
                        None,
                    )
                    if primary:
                        info["email"] = primary["email"]
            except Exception:                                      # pragma: no cover
                pass
        return info


async def _consume_state(db: AsyncSession, state: str) -> OAuthState:
    row = await db.scalar(select(OAuthState).where(OAuthState.state == state))
    if not row:
        raise ValueError("Unknown state parameter.")
    if row.used_at is not None:
        raise ValueError("State already consumed.")
    if utc_now() - row.created_at > timedelta(minutes=10):
        raise ValueError("State expired.")
    row.used_at = utc_now()
    return row


async def _find_or_create_user(
    db: AsyncSession,
    provider_name: str,
    profile: dict[str, Any],
    token_payload: dict[str, Any],
) -> tuple[User, bool]:
    """Link / create local user from the canonical profile."""
    from app.core.share_code import generate_share_code

    provider_user_id = profile.get("provider_user_id") or ""
    email = (profile.get("email") or "").strip().lower() or None
    name = profile.get("name") or "User"

    # 1) Match existing OAuth account by (provider, sub)
    existing_oa = await db.scalar(
        select(OAuthAccount).where(
            OAuthAccount.provider == provider_name,
            OAuthAccount.provider_user_id == provider_user_id,
        )
    )
    if existing_oa:
        user = await db.get(User, existing_oa.user_id)
        if user:
            _update_oa(existing_oa, profile, token_payload)
            return user, False

    # 2) If email matches an existing user, link
    existing_user: Optional[User] = None
    if email:
        existing_user = await db.scalar(
            select(User).where(User.username == email)
        )

    if existing_user:
        oa = OAuthAccount(
            user_id=existing_user.id,
            provider=provider_name,
            provider_user_id=provider_user_id,
            email=email,
            name=name,
            avatar_url=profile.get("avatar_url"),
            raw_profile=profile.get("raw") or {},
        )
        _update_oa(oa, profile, token_payload)
        db.add(oa)
        await db.flush()
        return existing_user, False

    # 3) Create a brand-new local user
    username = email or f"{provider_name}_{provider_user_id}"[:60]
    # ensure uniqueness on the username column
    suffix = 0
    while True:
        existing = await db.scalar(select(User).where(User.username == username))
        if not existing:
            break
        suffix += 1
        username = f"{username}_{suffix}"
        if suffix > 999:                                            # pragma: no cover
            username = f"{username}_{secrets.token_hex(3)}"
            break

    new_user = User(
        username=username,
        display_name=name,
        password_hash="!OAUTH-ONLY!",   # sentinel — local login is disabled
        share_code=generate_share_code(),
        avatar_url=profile.get("avatar_url"),
        status="online",
    )
    db.add(new_user)
    await db.flush()

    oa = OAuthAccount(
        user_id=new_user.id,
        provider=provider_name,
        provider_user_id=provider_user_id,
        email=email,
        name=name,
        avatar_url=profile.get("avatar_url"),
        raw_profile=profile.get("raw") or {},
    )
    _update_oa(oa, profile, token_payload)
    db.add(oa)
    await db.flush()
    return new_user, True


def _update_oa(
    oa: OAuthAccount,
    profile: dict[str, Any],
    token_payload: dict[str, Any],
) -> None:
    oa.email = profile.get("email") or oa.email
    oa.name = profile.get("name") or oa.name
    oa.avatar_url = profile.get("avatar_url") or oa.avatar_url
    oa.raw_profile = profile.get("raw") or oa.raw_profile
    oa.access_token = token_payload.get("access_token") or oa.access_token
    oa.refresh_token = token_payload.get("refresh_token") or oa.refresh_token
    exp = token_payload.get("expires_in")
    if isinstance(exp, (int, float)) and exp > 0:
        oa.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(exp))


async def handle_callback(
    db: AsyncSession,
    provider_name: str,
    code: str,
    state: str,
    *,
    code_verifier_override: Optional[str] = None,
) -> dict[str, Any]:
    provider = get_provider(provider_name)
    if not provider:
        raise ValueError(f"provider '{provider_name}' not configured.")

    state_row = await _consume_state(db, state)
    if state_row.provider != provider_name.lower():
        raise ValueError("Provider mismatch with state.")
    code_verifier = code_verifier_override or state_row.code_verifier

    token = await _exchange_code(
        provider, code, state_row.redirect_uri, code_verifier,
    )
    access_token = token.get("access_token")
    if not access_token:
        raise ValueError(
            f"Token endpoint returned no access_token (response keys: "
            f"{list(token.keys())})"
        )

    raw_profile = await _fetch_userinfo(provider, access_token)
    profile = provider.parse_userinfo(raw_profile)

    user, is_new = await _find_or_create_user(db, provider_name, profile, token)

    audit_log(
        "oauth.login", user_id=user.id, success=True,
        details={"provider": provider_name, "is_new_user": is_new},
    )

    return {
        "user": user,
        "is_new_user": is_new,
        "access_token": create_access_token(user.id, role=user.role),
        "refresh_token": create_refresh_token(user.id),
        "provider": provider_name,
    }


# ── SAML 2.0 wrapper (degraded) ─────────────────────────────

def saml_available() -> bool:
    try:
        import onelogin.saml2  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


async def saml_metadata(idp_name: str) -> dict[str, Any]:
    """Returns SAML SP metadata. Requires: pip install python3-saml."""
    if not saml_available():
        return {
            "status": "unavailable",
            "detail": "python3-saml is not installed; install it to enable SAML SSO.",
        }
    # Real impl would call onelogin.saml2.Settings + return XML metadata.
    return {"status": "ok", "idp": idp_name, "metadata_xml": ""}


async def saml_login(idp_name: str, relay_state: str) -> dict[str, Any]:
    if not saml_available():
        return {
            "status": "unavailable",
            "detail": "python3-saml is not installed.",
        }
    return {"status": "ok", "redirect_url": "", "relay_state": relay_state}
