"""
Phase 3 / Module N — OAuth2 / OIDC provider registry.

Each provider implements a small Protocol so the flow code is fully
provider-agnostic. Adding a new provider = subclass ``BaseOAuthProvider``,
override ``parse_userinfo``, and register it in ``_PROVIDERS``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class ProviderConfig:
    name: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scopes: list[str] = field(default_factory=list)
    extra_params: dict[str, str] = field(default_factory=dict)


class OAuthProviderProto(Protocol):
    name: str

    @property
    def authorize_url(self) -> str: ...
    @property
    def token_url(self) -> str: ...
    @property
    def userinfo_url(self) -> str: ...
    @property
    def scopes(self) -> list[str]: ...
    def parse_userinfo(self, raw: dict[str, Any]) -> dict[str, Any]: ...


class BaseOAuthProvider:
    name: str = "generic"

    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg

    @property
    def client_id(self) -> str:
        return self.cfg.client_id

    @property
    def client_secret(self) -> str:
        return self.cfg.client_secret

    @property
    def authorize_url(self) -> str:
        return self.cfg.authorize_url

    @property
    def token_url(self) -> str:
        return self.cfg.token_url

    @property
    def userinfo_url(self) -> str:
        return self.cfg.userinfo_url

    @property
    def scopes(self) -> list[str]:
        return list(self.cfg.scopes)

    @property
    def extra_params(self) -> dict[str, str]:
        return dict(self.cfg.extra_params)

    # subclasses override to project provider-specific fields to a
    # canonical schema: {provider_user_id, email, name, avatar_url}
    def parse_userinfo(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider_user_id": str(raw.get("id") or raw.get("sub") or ""),
            "email": raw.get("email"),
            "name": raw.get("name") or raw.get("login") or raw.get("preferred_username"),
            "avatar_url": raw.get("picture") or raw.get("avatar_url"),
            "raw": raw,
        }


class GoogleProvider(BaseOAuthProvider):
    name = "google"

    def parse_userinfo(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider_user_id": str(raw.get("sub") or raw.get("id") or ""),
            "email": raw.get("email"),
            "name": raw.get("name"),
            "avatar_url": raw.get("picture"),
            "raw": raw,
        }


class MicrosoftProvider(BaseOAuthProvider):
    name = "microsoft"

    def parse_userinfo(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider_user_id": str(raw.get("id") or raw.get("sub") or ""),
            "email": raw.get("mail") or raw.get("userPrincipalName") or raw.get("email"),
            "name": raw.get("displayName") or raw.get("name"),
            "avatar_url": None,
            "raw": raw,
        }


class GitHubProvider(BaseOAuthProvider):
    name = "github"

    def parse_userinfo(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider_user_id": str(raw.get("id") or ""),
            "email": raw.get("email"),
            "name": raw.get("name") or raw.get("login"),
            "avatar_url": raw.get("avatar_url"),
            "raw": raw,
        }


class GenericOIDCProvider(BaseOAuthProvider):
    name = "oidc"

    def parse_userinfo(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider_user_id": str(raw.get("sub") or raw.get("id") or ""),
            "email": raw.get("email"),
            "name": raw.get("name") or raw.get("preferred_username"),
            "avatar_url": raw.get("picture"),
            "raw": raw,
        }


# ── Registry ────────────────────────────────────────────────

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _build_default_registry() -> dict[str, BaseOAuthProvider]:
    out: dict[str, BaseOAuthProvider] = {}

    if _env("OAUTH_GOOGLE_CLIENT_ID"):
        out["google"] = GoogleProvider(ProviderConfig(
            name="google",
            client_id=_env("OAUTH_GOOGLE_CLIENT_ID"),
            client_secret=_env("OAUTH_GOOGLE_CLIENT_SECRET"),
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
            scopes=["openid", "email", "profile"],
            extra_params={"access_type": "offline", "prompt": "consent"},
        ))

    if _env("OAUTH_MICROSOFT_CLIENT_ID"):
        tenant = _env("OAUTH_MICROSOFT_TENANT", "common")
        out["microsoft"] = MicrosoftProvider(ProviderConfig(
            name="microsoft",
            client_id=_env("OAUTH_MICROSOFT_CLIENT_ID"),
            client_secret=_env("OAUTH_MICROSOFT_CLIENT_SECRET"),
            authorize_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
            token_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            userinfo_url="https://graph.microsoft.com/oidc/userinfo",
            scopes=["openid", "email", "profile", "offline_access"],
        ))

    if _env("OAUTH_GITHUB_CLIENT_ID"):
        out["github"] = GitHubProvider(ProviderConfig(
            name="github",
            client_id=_env("OAUTH_GITHUB_CLIENT_ID"),
            client_secret=_env("OAUTH_GITHUB_CLIENT_SECRET"),
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            userinfo_url="https://api.github.com/user",
            scopes=["read:user", "user:email"],
        ))

    if _env("OAUTH_OIDC_CLIENT_ID") and _env("OAUTH_OIDC_AUTHORIZE_URL"):
        out["oidc"] = GenericOIDCProvider(ProviderConfig(
            name="oidc",
            client_id=_env("OAUTH_OIDC_CLIENT_ID"),
            client_secret=_env("OAUTH_OIDC_CLIENT_SECRET"),
            authorize_url=_env("OAUTH_OIDC_AUTHORIZE_URL"),
            token_url=_env("OAUTH_OIDC_TOKEN_URL"),
            userinfo_url=_env("OAUTH_OIDC_USERINFO_URL"),
            scopes=_env("OAUTH_OIDC_SCOPES", "openid email profile").split(),
        ))

    return out


_PROVIDERS: dict[str, BaseOAuthProvider] = _build_default_registry()


def get_provider(name: str) -> Optional[BaseOAuthProvider]:
    return _PROVIDERS.get(name.lower())


def list_providers() -> list[str]:
    return sorted(_PROVIDERS.keys())


def register_provider(provider: BaseOAuthProvider) -> None:
    _PROVIDERS[provider.name.lower()] = provider
