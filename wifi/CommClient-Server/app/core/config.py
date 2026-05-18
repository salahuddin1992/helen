"""
Application configuration loaded from environment variables.
Single source of truth for all settings.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings with environment variable binding."""

    # ── Server ──────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 3000
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    # In PyInstaller frozen builds, __file__ resolves inside _internal/
    # Use sys._MEIPASS if available, otherwise standard path resolution
    PROJECT_ROOT: Path = Path(
        getattr(__import__('sys'), '_MEIPASS', None)
        or str(Path(__file__).resolve().parent.parent.parent)
    )

    # ── Database ────────────────────────────────────────────
    DB_BACKEND: str = "sqlite"  # "sqlite" or "postgresql"
    SQLITE_PATH: str = "./data/commclient.db"
    DATABASE_URL: str | None = None  # For PostgreSQL

    # ── JWT ─────────────────────────────────────────────────
    JWT_SECRET: str = Field(default_factory=lambda: secrets.token_hex(32))
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── Discovery ───────────────────────────────────────────
    SERVER_NAME: str = "Helen Server"
    DISCOVERY_UDP_PORT: int = 41234
    DISCOVERY_BROADCAST_INTERVAL: int = 3

    # ── Raw TCP fallback listener ───────────────────────────
    # A tiny line-oriented TCP server that exposes HELLO / PING /
    # DISCOVER / STATUS commands. Used as a fallback path when the
    # HTTP+WebSocket transport is blocked; clients can still confirm
    # server presence and fetch connection metadata.
    TCP_FALLBACK_ENABLED: bool = True
    TCP_FALLBACK_PORT: int = 41235

    # ── Media Ports (reserved for mediasoup) ────────────────
    MEDIASOUP_MIN_PORT: int = 40000
    MEDIASOUP_MAX_PORT: int = 49999

    # ── ICE / STUN / TURN ───────────────────────────────────
    # Public/announced IP used in SDP — overrides auto-detected LAN IP
    # when set (useful for multi-homed hosts or when binding behind NAT).
    ICE_ANNOUNCED_IP: str | None = None
    # Comma-separated STUN URIs exposed to clients. Defaults to an empty
    # string; at runtime we fall back to the local STUN (served by our
    # TURN service) bound to the detected LAN IP if this is blank.
    STUN_URIS: str = ""
    # Default STUN port when we auto-build the URI from a LAN IP.
    STUN_PORT: int = 3478
    # Comma-separated TURN URIs exposed to clients. Same fallback as STUN —
    # if blank, we auto-build turn:<lan_ip>:<TURN_PORT>?transport=udp,
    # turn:<lan_ip>:<TURN_PORT>?transport=tcp and turns:<lan_ip>:<TURN_TLS_PORT>.
    TURN_URIS: str = ""
    TURN_PORT: int = 3478
    TURN_TLS_PORT: int = 5349
    # Include TLS (turns:) URI in the returned ICE servers list.
    TURN_ENABLE_TLS: bool = False
    # Force relay-only candidates (iceTransportPolicy="relay") — useful to
    # verify TURN path in QA. Leave False for production.
    ICE_FORCE_RELAY: bool = False
    # TTL of the ephemeral TURN credentials handed out with each call.
    TURN_CREDENTIAL_TTL_SECONDS: int = 3600

    # ── File Uploads ────────────────────────────────────────
    UPLOAD_DIR: str = "./data/files"
    # 0 = unlimited (use resumable endpoint for large files to avoid OOM).
    MAX_UPLOAD_SIZE_MB: int = 0
    # Empty string = allow every extension.
    ALLOWED_EXTENSIONS: str = ""

    # ── Logging ──────────────────────────────────────────────
    LOG_DIR: str = ""  # Empty = default; set to absolute path from Electron

    # ── HTTPS (optional) ─────────────────────────────────────
    # When enabled, uvicorn binds TLS using the cert pair resolved by
    # `ssl_paths` below. If the files don't exist, a self-signed cert
    # is auto-generated on first start — useful for LAN where no CA
    # chain is available. Clients must accept self-signed certs
    # (Electron: `ses.setCertificateVerifyProc`).
    HTTPS_ENABLED: bool = False
    SSL_CERTFILE: str = ""   # Empty = <data>/certs/helen.crt
    SSL_KEYFILE: str = ""    # Empty = <data>/certs/helen.key
    # Extra SANs for the generated cert. LAN IP + hostname are added
    # automatically; this list adds anything else clients might use.
    SSL_EXTRA_SANS: str = ""

    # ── Rate Limiting ───────────────────────────────────────
    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_GENERAL: str = "100/minute"
    # Global HTTP rate limiter (token-bucket, per-user-or-IP) — applied to
    # every API endpoint. Set RATE_LIMIT_GLOBAL_ENABLED=false to disable
    # entirely during load-testing. RATE_LIMIT_TRUST_LAN=true (default)
    # whitelists 10/8, 172.16-31/12, 192.168/16, and loopback so LAN
    # deployments don't 429 themselves.
    RATE_LIMIT_GLOBAL_ENABLED: bool = True
    RATE_LIMIT_TRUST_LAN: bool = True

    # ── Session limits ──────────────────────────────────────
    # Cap how many active sessions one user can hold. On login, if the
    # count is already at MAX, the oldest session is revoked to make
    # room — so a stolen token can't silently accumulate sessions.
    # 0 = unlimited (not recommended).
    MAX_SESSIONS_PER_USER: int = 10

    # ── Automated Backups ───────────────────────────────────
    # Background scheduler makes periodic SQLite snapshots into data/backups/
    # and prunes to RETAIN_COUNT most recent. Entirely local — no network.
    # Disable if you run a managed backup solution externally.
    AUTO_BACKUP_ENABLED: bool = True
    AUTO_BACKUP_INTERVAL_HOURS: float = 6.0      # every 6 hours by default
    AUTO_BACKUP_RETAIN_COUNT: int = 14           # keep ~3.5 days at 6h cadence
    AUTO_BACKUP_STARTUP_DELAY_SEC: int = 120     # wait this long after boot

    # ── File Upload Throttling ──────────────────────────────
    # Max file uploads per user in a rolling window.
    UPLOAD_RATE_MAX_FILES: int = 30         # uploads
    UPLOAD_RATE_WINDOW_SEC: int = 60        # per minute
    # Max total bytes uploaded per user in the same window. 0 = unlimited.
    UPLOAD_RATE_MAX_BYTES: int = 0
    # Max concurrent in-flight uploads per user. 0 = unlimited.
    UPLOAD_MAX_CONCURRENT: int = 0

    # ── Federation (cross-server user lookup + signaling relay) ──
    # When multiple Helen servers sit on a LAN (or are bridged together),
    # they can cooperate: a user registered on server A can be found and
    # messaged by someone registered on server D. To prevent arbitrary hosts
    # on the LAN from injecting events, every inter-server request is signed
    # with this shared HMAC secret. Operators set the SAME value on all
    # servers that should federate. Leave blank to disable federation.
    # Federation defaults to ON for LAN-only deployments. The secret
    # is auto-derived from COMMCLIENT_CLUSTER_ID when blank (see
    # federation_auth._effective_secret) so two Helen-Servers on the
    # same cluster_id can federate with zero config. Operators that
    # want stronger isolation can pin an explicit FEDERATION_SECRET.
    FEDERATION_ENABLED: bool = True
    FEDERATION_SECRET: str = ""
    # Max clock skew tolerated on incoming federation requests (replay window).
    FEDERATION_REPLAY_WINDOW_SECONDS: int = 60
    # Timeout per peer HTTP call. Kept short so one slow peer doesn't stall
    # a lookup fanning out to every neighbor.
    FEDERATION_PEER_TIMEOUT_SECONDS: float = 2.0
    # Max concurrent UDP relay sessions on this server (ingress ports).
    # Each session owns 2 sockets; a machine with the default 16k ephemeral
    # port range can theoretically take many more, but we cap well under
    # that so a single abusive peer can't exhaust the range.
    FEDERATION_MAX_RELAY_SESSIONS: int = 2048
    # Per-peer relay quota. Even if the global cap isn't hit, no single
    # peer can hold more than this many active sessions at once.
    FEDERATION_PER_PEER_RELAY_QUOTA: int = 256
    # Token-bucket rate limit on /federation/relay/alloc per peer:
    # `RATE` new allocations per second, bursting up to `BURST`.
    FEDERATION_RELAY_ALLOC_RATE_PER_SEC: float = 10.0
    FEDERATION_RELAY_ALLOC_BURST: int = 30
    # Circuit breaker — stop fanning out to a peer after N consecutive
    # failures. The breaker re-closes after OPEN_SECONDS of cooldown on
    # the next successful probe.
    FEDERATION_BREAKER_FAIL_THRESHOLD: int = 5
    FEDERATION_BREAKER_OPEN_SECONDS: float = 30.0

    # ── Peer Acceptance Modes ─────────────────────────────────────
    # Behaviour when a new CommClient-Server is discovered on the
    # network (LAN/VPN/cluster). Four modes:
    #
    #   auto_accept       — verify-then-trust. Failed verify = reject.
    #                        Successful verify = immediately routable.
    #   manual_approval   — verify, then PARK in WAITING_MANUAL_APPROVAL
    #                        until an admin approves. Default for prod.
    #   pending_approval  — verify, then list in /api/admin/peers/pending.
    #                        Same gate as manual_approval but separate
    #                        UX (admin reviews queue out-of-band).
    #   human_selection   — verify, then list as AWAITING_HUMAN_SELECTION
    #                        for the admin to make a per-peer accept/
    #                        reject/deny/ignore/trust-once/trust-permanent
    #                        decision. Most defensive mode.
    #
    # The verification step (HMAC + cluster_id + nonce + timestamp +
    # version + capabilities) is the same in ALL modes — the modes
    # differ only in what happens AFTER successful verification.
    # Default = auto_accept so two Helen-Servers on the same LAN
    # federate the moment they discover each other. Admin can flip
    # the live sync_policy to pause federation or block specific
    # peers without editing config or restarting (see
    # /api/admin/peers/sync-policy).
    COMMCLIENT_PEER_ACCEPTANCE_MODE: str = "auto_accept"
    COMMCLIENT_REQUIRE_PEER_AUTH: bool = True
    COMMCLIENT_REQUIRE_CLUSTER_ID_MATCH: bool = True
    COMMCLIENT_REQUIRE_SIGNATURE: bool = True
    COMMCLIENT_REQUIRE_REPLAY_PROTECTION: bool = True
    # The cluster_id this server belongs to. Peers with a different
    # cluster_id are rejected outright (cluster isolation).
    COMMCLIENT_CLUSTER_ID: str = "default"
    # How long a peer can sit in PENDING/WAITING_MANUAL_APPROVAL before
    # auto-eviction. 24h default; lab/test setups can lower this.
    COMMCLIENT_PEER_PENDING_TTL_SECONDS: int = 86_400
    # How long a denied peer's signature stays in the deny cache so a
    # repeated discovery doesn't immediately re-trigger the approval flow.
    COMMCLIENT_PEER_DENY_CACHE_SECONDS: int = 300
    # Always audit-log every peer approval / rejection / deny / ignore.
    COMMCLIENT_PEER_APPROVAL_AUDIT_LOG: bool = True
    # Whether each mode is allowed (operator can hard-disable a mode).
    COMMCLIENT_ALLOW_AUTO_ACCEPT: bool = True
    COMMCLIENT_ALLOW_MANUAL_APPROVAL: bool = True
    COMMCLIENT_ALLOW_PENDING_APPROVAL: bool = True
    COMMCLIENT_ALLOW_HUMAN_SELECTION: bool = True

    # ── Leader Election (multi-worker singleton background loops) ──
    # Backend selection for ``app.services.leader_election``.
    # - "single":   single-process (dev/SQLite) — always leader.
    # - "postgres": pg_try_advisory_lock — requires DB_BACKEND=postgresql.
    # - "redis":    SET NX PX heartbeat — requires REDIS_URL.
    # - None:       auto-detect from DB_BACKEND.
    LEADER_ELECTION_BACKEND: str | None = None
    # Redis URL for the Redis leader-election backend (and future caches).
    # Example: redis://:password@host:6379/0
    REDIS_URL: str | None = None
    # Default lease TTL for leader-gated loops; heartbeats run at 50%.
    LEADER_LEASE_TTL_SECONDS: int = 60

    @property
    def log_path(self) -> Path:
        """Resolve the log directory — supports absolute paths from Electron."""
        if self.LOG_DIR:
            log_dir = Path(self.LOG_DIR)
            if log_dir.is_absolute():
                p = log_dir.resolve()
            else:
                p = (self.PROJECT_ROOT / self.LOG_DIR).resolve()
        else:
            # Default: sibling of data directory
            sqlite_parent = Path(self.SQLITE_PATH).parent
            if sqlite_parent.is_absolute():
                p = sqlite_parent.parent / "logs"
            else:
                p = self.PROJECT_ROOT / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def ssl_paths(self) -> tuple[Path, Path]:
        """Resolve (certfile, keyfile). Creates parent dir but not the files."""
        def _resolve(rel: str, default_name: str) -> Path:
            if rel:
                p = Path(rel)
                return p.resolve() if p.is_absolute() else (self.PROJECT_ROOT / p).resolve()
            # Default: sibling "certs" dir next to the data dir, same
            # pattern as log_path — keeps all operator artefacts together.
            sqlite_parent = Path(self.SQLITE_PATH).parent
            base = sqlite_parent.parent if sqlite_parent.is_absolute() else self.PROJECT_ROOT
            return (base / "certs" / default_name).resolve()

        certfile = _resolve(self.SSL_CERTFILE, "helen.crt")
        keyfile = _resolve(self.SSL_KEYFILE, "helen.key")
        certfile.parent.mkdir(parents=True, exist_ok=True)
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        return certfile, keyfile

    @property
    def db_url(self) -> str:
        """Resolve the database URL based on backend choice."""
        if self.DB_BACKEND == "postgresql" and self.DATABASE_URL:
            return self.DATABASE_URL
        sqlite_path = Path(self.SQLITE_PATH)
        # Support absolute paths (passed from Electron in production)
        if sqlite_path.is_absolute():
            db_path = sqlite_path.resolve()
        else:
            db_path = (self.PROJECT_ROOT / self.SQLITE_PATH).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{db_path}"

    @property
    def upload_path(self) -> Path:
        upload = Path(self.UPLOAD_DIR)
        # Support absolute paths (passed from Electron in production)
        if upload.is_absolute():
            p = upload.resolve()
        else:
            p = (self.PROJECT_ROOT / self.UPLOAD_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def allowed_ext_set(self) -> set[str]:
        return {e.strip().lower() for e in self.ALLOWED_EXTENSIONS.split(",") if e.strip()}

    @property
    def max_upload_bytes(self) -> int:
        # 0 means unlimited — return a sentinel larger than any real file.
        if self.MAX_UPLOAD_SIZE_MB <= 0:
            return 2**63 - 1
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        # Allow ad-hoc env vars (HELEN_ROUTER_*, HELEN_REQUIRE_ROUTER,
        # HELEN_DISABLE_BROADCAST, etc.) to flow through without forcing
        # us to declare every one as a typed field. Code that needs
        # these reads them directly via ``os.environ.get``.
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
