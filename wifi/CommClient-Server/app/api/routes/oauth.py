"""
Phase 3 / Module N — OAuth2 / OIDC REST endpoints.

Routes
------
GET    /api/oauth/providers                — list configured providers
GET    /api/oauth/{provider}/authorize     — kick off authorization
GET    /api/oauth/{provider}/callback      — browser callback handler
POST   /api/oauth/{provider}/desktop/exchange — Electron PKCE exchange
GET    /api/users/me/oauth-accounts        — list linked accounts
DELETE /api/users/me/oauth-accounts/{id}   — unlink
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_log
from app.core.deps import get_current_user_id, get_db
from app.core.logging import get_logger
from app.models.oauth import OAuthAccount
from app.services.oauth import flow as oauth_flow
from app.services.oauth.providers import get_provider, list_providers

logger = get_logger(__name__)

router = APIRouter(tags=["oauth"])


# ── Shapes ─────────────────────────────────────────────────

class AuthorizeOut(BaseModel):
    authorize_url: str
    state: str
    code_verifier: Optional[str] = None
    provider: str


class DesktopExchangeIn(BaseModel):
    code: str
    state: str
    code_verifier: Optional[str] = None


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    is_new_user: bool
    provider: str


class OAuthAccountOut(BaseModel):
    id: str
    provider: str
    provider_user_id: str
    email: Optional[str]
    name: Optional[str]
    avatar_url: Optional[str]
    created_at: datetime


class ProviderInfoOut(BaseModel):
    name: str
    scopes: list[str]


# ── Routes ─────────────────────────────────────────────────

@router.get("/api/oauth/providers", response_model=list[ProviderInfoOut])
async def list_configured_providers() -> list[ProviderInfoOut]:
    out: list[ProviderInfoOut] = []
    for name in list_providers():
        p = get_provider(name)
        if p:
            out.append(ProviderInfoOut(name=p.name, scopes=p.scopes))
    return out


@router.get("/api/oauth/{provider}/authorize", response_model=AuthorizeOut)
async def authorize(
    provider: str,
    redirect_uri: str = Query(...),
    desktop: int = Query(0),
    db: AsyncSession = Depends(get_db),
) -> AuthorizeOut:
    try:
        payload = await oauth_flow.start_authorization(
            db, provider, redirect_uri, desktop=bool(desktop),
        )
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return AuthorizeOut(
        authorize_url=payload["authorize_url"],
        state=payload["state"],
        code_verifier=payload["code_verifier"],
        provider=provider,
    )


@router.get("/api/oauth/{provider}/callback")
async def callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await oauth_flow.handle_callback(db, provider, code, state)
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:                                       # pragma: no cover
        await db.rollback()
        logger.error("oauth_callback_error", provider=provider, error=str(exc))
        raise HTTPException(status_code=502, detail="OAuth provider error")

    user = result["user"]
    access = result["access_token"]
    refresh = result["refresh_token"]

    # Two-channel response: an HTML page that postMessages tokens to the
    # opening window (browser flow) AND a JSON fallback for headless tests
    # via the Accept header.
    html = f"""<!doctype html>
<html><body><script>
  const data = {{
    type: 'helen-oauth',
    provider: {provider!r},
    access_token: {access!r},
    refresh_token: {refresh!r},
    user_id: {user.id!r},
    is_new_user: {str(result["is_new_user"]).lower()}
  }};
  try {{ window.opener && window.opener.postMessage(data, '*'); }} catch(e){{}}
  document.body.innerText = 'Login complete. You can close this window.';
  setTimeout(()=>window.close(), 1500);
</script></body></html>"""
    return HTMLResponse(content=html)


@router.post("/api/oauth/{provider}/desktop/exchange", response_model=TokenPairOut)
async def desktop_exchange(
    provider: str,
    body: DesktopExchangeIn,
    db: AsyncSession = Depends(get_db),
) -> TokenPairOut:
    try:
        result = await oauth_flow.handle_callback(
            db, provider, body.code, body.state,
            code_verifier_override=body.code_verifier,
        )
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    user = result["user"]
    return TokenPairOut(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        user_id=user.id,
        is_new_user=result["is_new_user"],
        provider=provider,
    )


# ── Linked accounts ─────────────────────────────────────────

@router.get("/api/users/me/oauth-accounts", response_model=list[OAuthAccountOut])
async def list_my_oauth_accounts(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[OAuthAccountOut]:
    rows = await db.scalars(
        select(OAuthAccount).where(OAuthAccount.user_id == user_id)
        .order_by(OAuthAccount.created_at.desc())
    )
    return [
        OAuthAccountOut(
            id=r.id, provider=r.provider, provider_user_id=r.provider_user_id,
            email=r.email, name=r.name, avatar_url=r.avatar_url,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.delete("/api/users/me/oauth-accounts/{account_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def unlink_oauth_account(
    account_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> None:
    acc = await db.get(OAuthAccount, account_id)
    if not acc or acc.user_id != user_id:
        raise HTTPException(status_code=404, detail="OAuth account not found.")
    await db.delete(acc)
    await db.commit()
    audit_log("oauth.account_unlinked", user_id=user_id, success=True,
              details={"provider": acc.provider, "account_id": account_id})
