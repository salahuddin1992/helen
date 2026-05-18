"""
LDAP / Active Directory single sign-on for Helen.

For organisations that already run AD or OpenLDAP, this module lets
users log in to Helen with their existing corporate credentials. No
separate password to manage, no separate group membership to sync —
both come from the directory.

Flow
----
  1. User sends username + password to ``/api/auth/login``.
  2. The route calls ``LDAPAuthenticator.authenticate(user, pwd)``.
  3. The authenticator:
       a. binds to the directory as ``bind_dn`` (service account)
       b. searches for the user by ``user_attr`` (default ``sAMAccountName``)
       c. re-binds as the found DN with the user's password
       d. on success, fetches group memberships via ``memberOf``
       e. returns an ``LDAPProfile`` (display name, mail, groups)
  4. Helen creates / updates a local user row mirroring the LDAP
     profile and issues a JWT as usual.

The directory connection is read-only — Helen never modifies AD.

Dependencies
------------
``ldap3`` is the python LDAP client we use. It's pure-Python (no
external libldap dep) so it ships fine inside the PyInstaller
bundle. Failed import gives a clear error so deployments without AD
just see the feature as disabled.

Configuration (env vars)
------------------------
  HELEN_LDAP_ENABLED             1 to turn on
  HELEN_LDAP_HOST                ldap-host.lan
  HELEN_LDAP_PORT                389 (LDAP) or 636 (LDAPS)
  HELEN_LDAP_USE_TLS             1 to STARTTLS on plain port
  HELEN_LDAP_USE_LDAPS           1 to use ldaps://
  HELEN_LDAP_BIND_DN             CN=helen-svc,OU=Service Accounts,DC=corp,DC=lan
  HELEN_LDAP_BIND_PASSWORD       service-account-password
  HELEN_LDAP_USER_BASE_DN        OU=Users,DC=corp,DC=lan
  HELEN_LDAP_USER_FILTER         (&(objectClass=user)(sAMAccountName={user}))
  HELEN_LDAP_GROUP_MAPPING_JSON  optional JSON {ldap_group_dn: helen_role}
  HELEN_LDAP_DEFAULT_ROLE        member (fallback if no group match)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LDAPProfile:
    username: str
    display_name: str
    email: Optional[str]
    dn: str
    groups: list[str] = field(default_factory=list)
    helen_role: str = "member"   # mapped via group → role table


@dataclass
class LDAPConfig:
    host: str
    port: int = 389
    use_starttls: bool = False
    use_ldaps: bool = False
    bind_dn: str = ""
    bind_password: str = ""
    user_base_dn: str = ""
    user_filter: str = "(&(objectClass=user)(sAMAccountName={user}))"
    user_attr_username: str = "sAMAccountName"
    user_attr_name: str = "displayName"
    user_attr_email: str = "mail"
    group_mapping: dict[str, str] = field(default_factory=dict)
    default_role: str = "member"

    @classmethod
    def from_env(cls) -> Optional["LDAPConfig"]:
        if os.environ.get("HELEN_LDAP_ENABLED", "").lower() not in (
                "1", "true", "yes"):
            return None
        host = os.environ.get("HELEN_LDAP_HOST", "").strip()
        if not host:
            return None
        try:
            mapping = json.loads(
                os.environ.get("HELEN_LDAP_GROUP_MAPPING_JSON", "{}"),
            )
            if not isinstance(mapping, dict):
                mapping = {}
        except Exception:
            mapping = {}

        port_default = 636 if (
            os.environ.get("HELEN_LDAP_USE_LDAPS", "").lower()
            in ("1", "true", "yes")
        ) else 389
        return cls(
            host=host,
            port=int(os.environ.get("HELEN_LDAP_PORT") or port_default),
            use_starttls=os.environ.get("HELEN_LDAP_USE_TLS", "").lower()
                in ("1", "true", "yes"),
            use_ldaps=os.environ.get("HELEN_LDAP_USE_LDAPS", "").lower()
                in ("1", "true", "yes"),
            bind_dn=os.environ.get("HELEN_LDAP_BIND_DN", ""),
            bind_password=os.environ.get("HELEN_LDAP_BIND_PASSWORD", ""),
            user_base_dn=os.environ.get("HELEN_LDAP_USER_BASE_DN", ""),
            user_filter=os.environ.get(
                "HELEN_LDAP_USER_FILTER",
                "(&(objectClass=user)(sAMAccountName={user}))",
            ),
            group_mapping=mapping,
            default_role=os.environ.get(
                "HELEN_LDAP_DEFAULT_ROLE", "member",
            ),
        )


class LDAPAuthError(RuntimeError):
    pass


class LDAPAuthenticator:

    def __init__(self, config: LDAPConfig) -> None:
        self.config = config

    def authenticate(self, username: str,
                      password: str) -> LDAPProfile:
        """Bind as the user, return their profile.

        Raises ``LDAPAuthError`` on bad credentials, missing user,
        or directory connectivity failure. Production code should
        treat any LDAPAuthError as "wrong password" to avoid
        leaking which usernames exist."""
        try:
            from ldap3 import (
                Server, Connection, Tls, ALL, SUBTREE,
                AUTO_BIND_NO_TLS, AUTO_BIND_TLS_BEFORE_BIND,
            )
            import ssl
        except ImportError as exc:
            raise LDAPAuthError(
                "ldap3 not installed — cannot authenticate via LDAP",
            ) from exc

        cfg = self.config
        scheme_url = "ldaps" if cfg.use_ldaps else "ldap"
        tls = None
        if cfg.use_starttls or cfg.use_ldaps:
            tls = Tls(validate=ssl.CERT_NONE)  # LAN deployment tolerance

        server = Server(
            f"{scheme_url}://{cfg.host}:{cfg.port}",
            tls=tls, get_info=ALL, connect_timeout=5,
        )

        # 1) Service-account bind for search
        try:
            svc = Connection(
                server,
                user=cfg.bind_dn or None,
                password=cfg.bind_password or None,
                auto_bind=(AUTO_BIND_TLS_BEFORE_BIND
                            if cfg.use_starttls else AUTO_BIND_NO_TLS),
                read_only=True,
            )
        except Exception as exc:
            raise LDAPAuthError(
                f"service-account bind failed: {exc}",
            ) from exc

        # 2) Search for the user — never interpolate untrusted
        #    input into the filter directly. ldap3 escapes
        #    via parameter expansion when we replace `{user}`
        #    with a sanitised version.
        safe = _ldap_escape(username)
        user_filter = cfg.user_filter.replace("{user}", safe)
        svc.search(
            search_base=cfg.user_base_dn,
            search_filter=user_filter,
            search_scope=SUBTREE,
            attributes=[cfg.user_attr_username, cfg.user_attr_name,
                         cfg.user_attr_email, "memberOf"],
        )
        if not svc.entries:
            svc.unbind()
            raise LDAPAuthError("unknown user")
        entry = svc.entries[0]
        user_dn = entry.entry_dn

        # 3) Re-bind as the user with their submitted password
        try:
            user_conn = Connection(
                server, user=user_dn, password=password,
                auto_bind=(AUTO_BIND_TLS_BEFORE_BIND
                            if cfg.use_starttls else AUTO_BIND_NO_TLS),
                read_only=True,
            )
        except Exception:
            svc.unbind()
            raise LDAPAuthError("authentication failed")

        groups = []
        try:
            groups = list(getattr(entry, "memberOf", []))
        except Exception:
            pass

        # 4) Map LDAP group → Helen role
        helen_role = cfg.default_role
        for group_dn in groups:
            mapped = cfg.group_mapping.get(str(group_dn))
            if mapped:
                # Most-privileged wins. Owner > admin > moderator > member.
                rank = {"member": 0, "moderator": 1, "admin": 2,
                         "owner": 3}
                if rank.get(mapped, 0) > rank.get(helen_role, 0):
                    helen_role = mapped

        profile = LDAPProfile(
            username=username,
            display_name=str(getattr(entry, cfg.user_attr_name, username)
                              or username),
            email=str(getattr(entry, cfg.user_attr_email, "") or "")
                  or None,
            dn=str(user_dn),
            groups=[str(g) for g in groups],
            helen_role=helen_role,
        )
        try:
            user_conn.unbind()
        except Exception:
            pass
        try:
            svc.unbind()
        except Exception:
            pass
        return profile


def _ldap_escape(value: str) -> str:
    """RFC 4515 escape — every reserved char becomes \\HEX."""
    repl = {
        "\\": r"\5c",
        "*": r"\2a",
        "(": r"\28",
        ")": r"\29",
        "\x00": r"\00",
        "/": r"\2f",
    }
    out = []
    for ch in value:
        out.append(repl.get(ch, ch))
    return "".join(out)
