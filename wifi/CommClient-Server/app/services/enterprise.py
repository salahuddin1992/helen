"""
Enterprise features for Helen v1.1: granular RBAC, moderation
toolkit, multi-device session management, webhooks.

Each subsystem is independent — admins can adopt one without the
others. Storage is SQLite for portability; for a high-write
deployment swap in PostgreSQL via the existing settings.DB_BACKEND.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# ── RBAC ────────────────────────────────────────────────────────────


# Capability flags — small enough to fit a 64-bit int. Extend by
# appending new entries; never reorder, never remove.
CAP_READ_MESSAGES        = 1 << 0
CAP_SEND_MESSAGES        = 1 << 1
CAP_DELETE_OWN_MESSAGES  = 1 << 2
CAP_DELETE_ANY_MESSAGES  = 1 << 3
CAP_CREATE_CHANNEL       = 1 << 4
CAP_DELETE_CHANNEL       = 1 << 5
CAP_INVITE_USERS         = 1 << 6
CAP_KICK_USERS           = 1 << 7
CAP_BAN_USERS            = 1 << 8
CAP_MUTE_USERS           = 1 << 9
CAP_PIN_MESSAGES         = 1 << 10
CAP_MANAGE_ROLES         = 1 << 11
CAP_VIEW_AUDIT_LOG       = 1 << 12
CAP_USE_WEBHOOKS         = 1 << 13
CAP_BYPASS_RATE_LIMIT    = 1 << 14
CAP_ADMIN                = 1 << 63   # superuser

# Predefined role bundles
ROLE_GUEST = (
    CAP_READ_MESSAGES
)
ROLE_MEMBER = (
    CAP_READ_MESSAGES | CAP_SEND_MESSAGES
    | CAP_DELETE_OWN_MESSAGES | CAP_INVITE_USERS
)
ROLE_MODERATOR = (
    ROLE_MEMBER | CAP_DELETE_ANY_MESSAGES | CAP_KICK_USERS
    | CAP_MUTE_USERS | CAP_PIN_MESSAGES | CAP_VIEW_AUDIT_LOG
)
ROLE_ADMIN = (
    ROLE_MODERATOR | CAP_BAN_USERS | CAP_CREATE_CHANNEL
    | CAP_DELETE_CHANNEL | CAP_MANAGE_ROLES | CAP_USE_WEBHOOKS
    | CAP_BYPASS_RATE_LIMIT
)
ROLE_OWNER = ROLE_ADMIN | CAP_ADMIN


@dataclass
class RoleAssignment:
    user_id: str
    channel_id: Optional[str]   # None = global role
    capabilities: int


class RBACStore:
    """Per-channel + global capability bits for every user."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with sqlite3.connect(db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS rbac_roles (
                    user_id     TEXT NOT NULL,
                    channel_id  TEXT,
                    capabilities INTEGER NOT NULL,
                    granted_by   TEXT,
                    granted_at   REAL NOT NULL,
                    PRIMARY KEY (user_id, channel_id)
                )
            """)

    def grant(self, user_id: str, capabilities: int,
              channel_id: Optional[str] = None,
              granted_by: Optional[str] = None) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO rbac_roles VALUES "
                "(?, ?, ?, ?, ?)",
                (user_id, channel_id, capabilities,
                 granted_by, time.time()),
            )

    def revoke(self, user_id: str,
                channel_id: Optional[str] = None) -> None:
        with sqlite3.connect(self.db_path) as c:
            if channel_id is None:
                c.execute("DELETE FROM rbac_roles "
                           "WHERE user_id=? AND channel_id IS NULL",
                           (user_id,))
            else:
                c.execute("DELETE FROM rbac_roles "
                           "WHERE user_id=? AND channel_id=?",
                           (user_id, channel_id))

    def can(self, user_id: str, capability: int,
            channel_id: Optional[str] = None) -> bool:
        """True iff user has ``capability``, considering both the
        channel-specific role (if any) and the global role.
        Channel-specific grants ADD to global ones; they don't
        replace. Global ADMIN always wins."""
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT capabilities FROM rbac_roles "
                "WHERE user_id=? AND channel_id IS NULL",
                (user_id,),
            ).fetchone()
            global_caps = row[0] if row else 0
            if global_caps & CAP_ADMIN:
                return True

            channel_caps = 0
            if channel_id is not None:
                row2 = c.execute(
                    "SELECT capabilities FROM rbac_roles "
                    "WHERE user_id=? AND channel_id=?",
                    (user_id, channel_id),
                ).fetchone()
                if row2:
                    channel_caps = row2[0]

            return bool((global_caps | channel_caps) & capability)

    def list_for_user(self, user_id: str) -> list[RoleAssignment]:
        with sqlite3.connect(self.db_path) as c:
            return [
                RoleAssignment(user_id=row[0], channel_id=row[1],
                                capabilities=row[2])
                for row in c.execute(
                    "SELECT user_id, channel_id, capabilities "
                    "FROM rbac_roles WHERE user_id=?",
                    (user_id,))
            ]


# ── Moderation ──────────────────────────────────────────────────────


@dataclass
class ModerationAction:
    action_id: str
    target_user: str
    by_moderator: str
    action: str          # kick | ban | mute | timeout | unban | warn
    reason: str = ""
    channel_id: Optional[str] = None
    expires_at: Optional[float] = None
    issued_at: float = field(default_factory=time.time)


class ModerationStore:
    """Append-only log of moderator actions + active mute/ban table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with sqlite3.connect(db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS moderation_log (
                    action_id    TEXT PRIMARY KEY,
                    target_user  TEXT NOT NULL,
                    by_moderator TEXT NOT NULL,
                    action       TEXT NOT NULL,
                    reason       TEXT,
                    channel_id   TEXT,
                    expires_at   REAL,
                    issued_at    REAL NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS "
                       "idx_modlog_target "
                       "ON moderation_log(target_user)")
            c.execute("CREATE INDEX IF NOT EXISTS "
                       "idx_modlog_channel "
                       "ON moderation_log(channel_id)")

    def record(self, action: ModerationAction) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO moderation_log VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?)",
                (action.action_id, action.target_user,
                 action.by_moderator, action.action, action.reason,
                 action.channel_id, action.expires_at,
                 action.issued_at),
            )

    def kick(self, target: str, by: str, *, reason: str = "",
              channel_id: Optional[str] = None) -> ModerationAction:
        a = ModerationAction(
            action_id=secrets.token_hex(8),
            target_user=target, by_moderator=by,
            action="kick", reason=reason, channel_id=channel_id,
        )
        self.record(a)
        return a

    def ban(self, target: str, by: str, *, reason: str = "",
             channel_id: Optional[str] = None,
             duration_sec: Optional[float] = None) -> ModerationAction:
        expires = (time.time() + duration_sec) if duration_sec else None
        a = ModerationAction(
            action_id=secrets.token_hex(8),
            target_user=target, by_moderator=by,
            action="ban", reason=reason, channel_id=channel_id,
            expires_at=expires,
        )
        self.record(a)
        return a

    def mute(self, target: str, by: str, *,
              duration_sec: float = 600,
              channel_id: Optional[str] = None,
              reason: str = "") -> ModerationAction:
        a = ModerationAction(
            action_id=secrets.token_hex(8),
            target_user=target, by_moderator=by,
            action="mute", reason=reason, channel_id=channel_id,
            expires_at=time.time() + duration_sec,
        )
        self.record(a)
        return a

    def _active_action(self, user_id: str, action: str,
                        channel_id: Optional[str]) -> bool:
        now = time.time()
        with sqlite3.connect(self.db_path) as c:
            if channel_id is None:
                row = c.execute("""
                    SELECT expires_at FROM moderation_log
                    WHERE target_user=? AND action=?
                    AND channel_id IS NULL
                    AND (expires_at IS NULL OR expires_at > ?)
                    ORDER BY issued_at DESC LIMIT 1
                """, (user_id, action, now)).fetchone()
            else:
                row = c.execute("""
                    SELECT expires_at FROM moderation_log
                    WHERE target_user=? AND action=?
                    AND (channel_id IS NULL OR channel_id=?)
                    AND (expires_at IS NULL OR expires_at > ?)
                    ORDER BY issued_at DESC LIMIT 1
                """, (user_id, action, channel_id, now)).fetchone()
            return row is not None

    def is_banned(self, user_id: str,
                   channel_id: Optional[str] = None) -> bool:
        return self._active_action(user_id, "ban", channel_id)

    def is_muted(self, user_id: str,
                  channel_id: Optional[str] = None) -> bool:
        return self._active_action(user_id, "mute", channel_id)


# ── Multi-device sessions ──────────────────────────────────────────


@dataclass
class DeviceSession:
    session_id: str
    user_id: str
    device_name: str          # "iPhone 15 — Safari", "MacBook — Helen Desktop"
    device_kind: str          # ios | android | windows | linux | macos | web
    ip_address: str
    user_agent: str
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    revoked_at: Optional[float] = None


class SessionStore:
    """One row per (user, device) — supports listing + remote revoke."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with sqlite3.connect(db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS device_sessions (
                    session_id      TEXT PRIMARY KEY,
                    user_id         TEXT NOT NULL,
                    device_name     TEXT,
                    device_kind     TEXT,
                    ip_address      TEXT,
                    user_agent      TEXT,
                    created_at      REAL NOT NULL,
                    last_active_at  REAL NOT NULL,
                    revoked_at      REAL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_sess_user "
                       "ON device_sessions(user_id)")

    def create(self, sess: DeviceSession) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("INSERT OR REPLACE INTO device_sessions VALUES "
                       "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (sess.session_id, sess.user_id, sess.device_name,
                        sess.device_kind, sess.ip_address,
                        sess.user_agent, sess.created_at,
                        sess.last_active_at, sess.revoked_at))

    def touch(self, session_id: str) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("UPDATE device_sessions SET last_active_at=? "
                       "WHERE session_id=?", (time.time(), session_id))

    def revoke(self, session_id: str) -> bool:
        with sqlite3.connect(self.db_path) as c:
            cur = c.execute(
                "UPDATE device_sessions SET revoked_at=? "
                "WHERE session_id=? AND revoked_at IS NULL",
                (time.time(), session_id),
            )
            return cur.rowcount > 0

    def revoke_all_for_user(self, user_id: str,
                              except_session: Optional[str] = None
                              ) -> int:
        with sqlite3.connect(self.db_path) as c:
            if except_session:
                cur = c.execute(
                    "UPDATE device_sessions SET revoked_at=? "
                    "WHERE user_id=? AND revoked_at IS NULL "
                    "AND session_id<>?",
                    (time.time(), user_id, except_session),
                )
            else:
                cur = c.execute(
                    "UPDATE device_sessions SET revoked_at=? "
                    "WHERE user_id=? AND revoked_at IS NULL",
                    (time.time(), user_id),
                )
            return cur.rowcount

    def is_revoked(self, session_id: str) -> bool:
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT revoked_at FROM device_sessions "
                "WHERE session_id=?", (session_id,),
            ).fetchone()
            return bool(row and row[0] is not None)

    def list_for_user(self, user_id: str) -> list[DeviceSession]:
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT session_id, user_id, device_name, device_kind, "
                "ip_address, user_agent, created_at, last_active_at, "
                "revoked_at FROM device_sessions "
                "WHERE user_id=? ORDER BY last_active_at DESC",
                (user_id,),
            ).fetchall()
        return [DeviceSession(*r) for r in rows]


# ── Webhooks ────────────────────────────────────────────────────────


@dataclass
class WebhookConfig:
    webhook_id: str
    owner_user: str
    name: str
    target_url: str          # internal LAN URL only
    events: list[str]        # ["message.create", "channel.create", …]
    secret: str = ""         # HMAC secret
    enabled: bool = True
    created_at: float = field(default_factory=time.time)


class WebhookStore:
    """Internal-LAN-only webhook subscription registry."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with sqlite3.connect(db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS webhooks (
                    webhook_id  TEXT PRIMARY KEY,
                    owner_user  TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    target_url  TEXT NOT NULL,
                    events      TEXT NOT NULL,
                    secret      TEXT,
                    enabled     INTEGER NOT NULL,
                    created_at  REAL NOT NULL
                )
            """)

    @staticmethod
    def _validate_lan_url(url: str) -> None:
        """Reject any webhook URL that doesn't resolve to RFC1918.
        This is the SSRF guard — without it a webhook could be used
        to scan / talk to public services from inside the server.
        """
        import ipaddress
        from urllib.parse import urlparse
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            raise ValueError(f"webhook scheme not allowed: {p.scheme}")
        host = p.hostname
        if not host:
            raise ValueError("webhook URL has no host")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # Hostname (not an IP literal) — only accept .lan / .local
            if not (host.endswith(".lan") or host.endswith(".local")
                    or host == "localhost"):
                raise ValueError(
                    f"webhook hostname must be IP, .lan, .local, or "
                    f"localhost; got {host}"
                )
            return
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            raise ValueError(f"webhook IP {ip} is not RFC1918")

    def create(self, owner_user: str, name: str,
                target_url: str, events: list[str]) -> WebhookConfig:
        self._validate_lan_url(target_url)
        wh = WebhookConfig(
            webhook_id=secrets.token_hex(8),
            owner_user=owner_user, name=name,
            target_url=target_url, events=events,
            secret=secrets.token_hex(32),
        )
        with sqlite3.connect(self.db_path) as c:
            c.execute("INSERT INTO webhooks VALUES "
                       "(?, ?, ?, ?, ?, ?, ?, ?)",
                       (wh.webhook_id, wh.owner_user, wh.name,
                        wh.target_url, json.dumps(wh.events),
                        wh.secret, 1, wh.created_at))
        return wh

    def all_for_event(self, event: str) -> list[WebhookConfig]:
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT webhook_id, owner_user, name, target_url, "
                "events, secret, enabled, created_at FROM webhooks "
                "WHERE enabled=1",
            ).fetchall()
        out: list[WebhookConfig] = []
        for r in rows:
            evs = json.loads(r[4])
            if event in evs or "*" in evs:
                out.append(WebhookConfig(
                    webhook_id=r[0], owner_user=r[1], name=r[2],
                    target_url=r[3], events=evs, secret=r[5],
                    enabled=bool(r[6]), created_at=r[7],
                ))
        return out

    def disable(self, webhook_id: str) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("UPDATE webhooks SET enabled=0 "
                       "WHERE webhook_id=?", (webhook_id,))
