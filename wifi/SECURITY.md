# CommClient — Threat Model & Security Architecture

## 1. Threat Model

### Attack Surface

| Surface | Protocol | Exposure | Threats |
|---------|----------|----------|---------|
| HTTP REST API (port 3000) | TCP | LAN hosts | API abuse, brute-force, injection, data exfiltration |
| Socket.IO (port 3000) | WS/TCP | LAN hosts | Room eavesdropping, event injection, session hijack |
| UDP Discovery (port 41234) | UDP | LAN broadcast | Server impersonation, discovery poisoning |
| WebRTC Media (40000-49999) | UDP/TCP | LAN hosts | Media interception (mitigated by DTLS-SRTP) |
| SQLite Database | Filesystem | Local machine | Data theft if machine compromised |
| Credential Store | Filesystem | Local machine | Token theft (mitigated by DPAPI) |

### Threat Actors (LAN Context)

| Actor | Capability | Motivation |
|-------|-----------|------------|
| **Malicious LAN User** | Authenticated CommClient user | Privilege escalation, eavesdropping, data tampering |
| **Rogue Device** | Unauthenticated host on LAN | Passive sniffing, server impersonation, DoS |
| **Compromised Client** | Full control of one Electron instance | Token theft, API replay, database extraction |

### Identified Threats & Mitigations

| ID | Threat | Severity | Status | Mitigation |
|----|--------|----------|--------|------------|
| T01 | Any user can access admin endpoints (kick/ban/backup/restore) | **CRITICAL** | **FIXED** | RBAC with `role` field on User model; `require_role("admin")` dependency |
| T02 | Any user can join any channel's group call without membership check | **CRITICAL** | **FIXED** | Channel membership verified in `call_join_group` and `v2_call_join_group` |
| T03 | CORS allows `file://` origin — local HTML can connect to socket | **HIGH** | **FIXED** | Removed `file://` from CORS origins; Electron uses `app://.` |
| T04 | Unified `call_signal` works without `call_id` — no participant check | **HIGH** | **FIXED** | `call_id` required; participant validation enforced |
| T05 | Typing/read/delivered indicators bypass channel membership | **MEDIUM** | **FIXED** | Membership check added to all v1 and v2 typing, read, delivered handlers |
| T06 | Message reactions don't verify channel membership | **MEDIUM** | **FIXED** | Channel membership check added in `toggle_reaction` |
| T07 | Pin/unpin messages allowed by any channel member (no admin check) | **MEDIUM** | **FIXED** | Admin/moderator role check added for pin/unpin |
| T08 | Sync operations accept arbitrary channel list without membership check | **MEDIUM** | **FIXED** | Channel membership filter in `v2_chat_sync` |
| T09 | JTI revocation set is in-memory, unbounded clear at 10k | **LOW** | **IMPROVED** | Switched to OrderedDict with LRU eviction (FIFO oldest) |
| T10 | JWT_SECRET auto-generated on restart invalidates all tokens | **MEDIUM** | **DOCUMENTED** | Startup warning if JWT_SECRET not explicitly set |
| T11 | No security event audit for socket authorization failures | **LOW** | **FIXED** | Audit logging added to all authorization rejection paths |
| T12 | `v2_call_initiate` doesn't validate target user exists | **LOW** | **FIXED** | User existence check added before call initiation |

## 2. Security Architecture

```
┌───────────────────────────────────────────────────────────────────────┐
│                        Electron Main Process                          │
│  ┌────────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │ DPAPI Credential   │  │ CSP Headers      │  │ Single Instance  │  │
│  │ Store (safeStorage)│  │ (restrict origin) │  │ Lock             │  │
│  └────────────────────┘  └──────────────────┘  └──────────────────┘  │
└───────────────────┬───────────────────────────────────────────────────┘
                    │ IPC (contextIsolation=true, sandbox=true)
┌───────────────────▼───────────────────────────────────────────────────┐
│                        Renderer Process                               │
│  ┌────────────────────┐  ┌──────────────────┐                        │
│  │ JWT Access Token   │  │ Socket.IO auth   │                        │
│  │ (memory only)      │  │ (token in auth{})│                        │
│  └────────┬───────────┘  └────────┬─────────┘                        │
└───────────┼───────────────────────┼──────────────────────────────────┘
            │ Bearer Token          │ WS auth payload
┌───────────▼───────────────────────▼──────────────────────────────────┐
│                    FastAPI + Socket.IO Server                          │
│                                                                        │
│  ┌─ Security Layer ───────────────────────────────────────────────┐   │
│  │                                                                 │   │
│  │  RequestIdMiddleware → SecurityHeadersMiddleware →              │   │
│  │  RequestSizeLimitMiddleware → CORS → Rate Limiting →           │   │
│  │  JWT Decode + Type Check + JTI Revocation Check →              │   │
│  │  RBAC Role Check (admin endpoints) →                           │   │
│  │  Channel Membership Verification (per-operation)               │   │
│  │                                                                 │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                        │
│  ┌─ Auth ──────────┐  ┌─ AuthZ ─────────────┐  ┌─ Audit ──────┐    │
│  │ Bcrypt (cost 12) │  │ RBAC: user/mod/admin│  │ Structured   │    │
│  │ JWT HS256 + JTI  │  │ Channel membership  │  │ security.audit│    │
│  │ Refresh rotation │  │ Call participation  │  │ logger        │    │
│  │ IP rate limiting │  │ File ownership      │  │               │    │
│  │ Account lockout  │  │ Message ownership   │  │               │    │
│  └──────────────────┘  └─────────────────────┘  └───────────────┘    │
│                                                                        │
│  ┌─ Storage ─────────────────────────────────────────────────────┐    │
│  │ SQLite (WAL mode) + AES-256-GCM field encryption              │    │
│  │ Refresh tokens: HMAC-SHA256 hashed before storage             │    │
│  │ File uploads: MIME validation + path sanitization              │    │
│  └───────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

## 3. RBAC Authorization Model

### Roles

| Role | Level | Capabilities |
|------|-------|-------------|
| `user` | 0 | Standard messaging, calls, file sharing, profile management |
| `moderator` | 1 | Everything in `user` + pin/unpin messages, mute users in calls |
| `admin` | 2 | Everything in `moderator` + kick/ban users, manage backups, server config, cleanup |

### Role Assignment
- First registered user → `admin` (bootstrap)
- Subsequent users → `user` (default)
- Admins can promote/demote via `/api/admin/set-role/{user_id}`
- Role stored in `users.role` column and embedded in JWT `role` claim

### Enforcement Points
- **REST API:** `require_role()` FastAPI dependency on admin/moderator routes
- **Socket.IO:** Channel-level `_require_admin()` on channel mutations
- **JWT:** Role claim checked at decode time; no DB lookup needed per request

## 4. Secure Storage Strategy

| Data | At Rest | In Transit | Access Control |
|------|---------|------------|----------------|
| Passwords | Bcrypt cost 12 | N/A (never transmitted) | Server-only |
| JWT Access Token | Memory only (no localStorage) | Bearer header / WS auth | 60min TTL + JTI revocation |
| JWT Refresh Token | HMAC-SHA256 hash in DB | HTTP-only body | 7-day TTL + rotation |
| Client Credentials | DPAPI (Windows) / Keychain (macOS) | IPC only | Electron safeStorage API |
| Message Content | Plaintext in SQLite | Plaintext over LAN WS | Channel membership gated |
| Uploaded Files | Plaintext on disk | HTTP with auth | Uploader + channel members |
| Sensitive DB Fields | AES-256-GCM (optional) | Encrypted payload | Field-level key from JWT_SECRET |

## 5. Input Validation Summary

| Input | Validation | Location |
|-------|-----------|----------|
| Username | `^[a-zA-Z0-9_.-]{3,64}$` | `auth.py:register` |
| Password | 8-128 chars, mixed types | `crypto.py:validate_password_strength` |
| Message content | Max 10,000 chars, stripped | `chat_handlers.py` + `message_service.py` |
| Emoji | String, max 8 chars | `chat_handlers.py` |
| File upload | MIME magic bytes + extension whitelist + 100MB limit | `file_service.py` |
| channel_id/user_id | UUID format validation | New: `security_utils.py:validate_uuid` |
| Backup name | No `..` path traversal | `admin.py:restore_backup` |
| Socket auth token | String, max 4096 chars | `server.py:connect` |
| Request body | 110MB max Content-Length | `middleware.py:RequestSizeLimitMiddleware` |
