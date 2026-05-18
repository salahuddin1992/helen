# CommClient — Threat Model

## 1. System Overview

CommClient is a LAN/WiFi-only desktop communication platform. All traffic stays within the local network segment. There is no cloud relay, no internet-facing endpoint, and no centralized identity provider. Authentication is local (username + password → JWT), media is P2P (WebRTC mesh), and signaling is relayed through a single FastAPI + Socket.IO server.

**Trust boundary:** the LAN itself. Any device on the same subnet can reach the server.

---

## 2. Assets

| Asset | Sensitivity | Storage |
|-------|------------|---------|
| User passwords (bcrypt hashes) | HIGH | SQLite `users.password_hash` |
| JWT access tokens (60 min TTL) | HIGH | Client localStorage, Socket.IO auth payload |
| JWT refresh tokens (7 day TTL) | CRITICAL | Client localStorage, SHA-256 hash in `user_sessions` |
| JWT signing secret | CRITICAL | `.env` / env var / config |
| Private messages (text) | HIGH | SQLite `messages`, plaintext |
| Uploaded files | MEDIUM-HIGH | Filesystem `data/files/` |
| Call signaling (SDP, ICE) | MEDIUM | In-memory relay, not persisted |
| Audio/video streams | HIGH | P2P WebRTC (DTLS-SRTP encrypted) |
| Screen-share streams | HIGH | P2P WebRTC (DTLS-SRTP encrypted) |
| User presence / status | LOW | In-memory dict |
| Channel membership | MEDIUM | SQLite `channel_members` |

---

## 3. Threat Actors

| Actor | Capability | Motivation |
|-------|-----------|-----------|
| **Rogue LAN user** | Full network access, can sniff unencrypted traffic, ARP-spoof, connect to server | Eavesdropping, impersonation, data theft |
| **Compromised client** | Has valid JWT, full API+Socket access | Lateral movement, exfiltration, abuse |
| **Physical access attacker** | Can read client filesystem, extract `%APPDATA%` | Token theft, offline password cracking |
| **Malicious insider** | Registered user with valid credentials | Privacy violation, data leaks, harassment |

---

## 4. Threat Matrix (STRIDE)

### 4.1 Spoofing / Impersonation

| # | Threat | Pre-fix status | Severity |
|---|--------|---------------|----------|
| S-1 | Attacker replays stolen JWT to impersonate user | **VULNERABLE** — no token blacklist, no binding to IP/fingerprint | CRITICAL |
| S-2 | Attacker modifies localStorage to inject forged tokens | **VULNERABLE** — tokens stored as plaintext JSON | HIGH |
| S-3 | Socket reconnection reuses expired token | **VULNERABLE** — no expiry re-check on reconnect | HIGH |
| S-4 | JWT signing key is static/weak across deployments | **VULNERABLE** — `.env` has hardcoded fallback | CRITICAL |

### 4.2 Tampering

| # | Threat | Pre-fix status | Severity |
|---|--------|---------------|----------|
| T-1 | Socket event payloads forged (call_id, user_id, channel_id) | **PARTIAL** — some validation, signal_offer logic bug allows bypass | HIGH |
| T-2 | File upload with spoofed extension (e.g., `.exe` renamed to `.jpg`) | **VULNERABLE** — extension-only check, no magic-byte validation | MEDIUM |
| T-3 | Message content injection (XSS via unsanitized HTML) | **PARTIAL** — React escapes by default, but `dangerouslySetInnerHTML` risk | MEDIUM |

### 4.3 Repudiation

| # | Threat | Pre-fix status | Severity |
|---|--------|---------------|----------|
| R-1 | No audit log — actions cannot be attributed after the fact | **VULNERABLE** — structlog exists but no security audit trail | MEDIUM |
| R-2 | Deleted messages are soft-deleted but no immutable audit | **ACCEPTABLE** — LAN context, regulatory compliance not required | LOW |

### 4.4 Information Disclosure

| # | Threat | Pre-fix status | Severity |
|---|--------|---------------|----------|
| I-1 | File download endpoint has NO authorization — any authenticated user can access any file | **CRITICAL VULNERABILITY** | CRITICAL |
| I-2 | Channel details (members, metadata) exposed to non-members | **VULNERABLE** — no membership check on `GET /channel/:id` | HIGH |
| I-3 | Error responses leak JWT decode errors to client | **VULNERABLE** — `detail=f"Invalid token: {e}"` | MEDIUM |
| I-4 | Messages marked-delivered by non-recipients | **VULNERABLE** — no recipient validation | HIGH |
| I-5 | Unencrypted HTTP traffic on LAN exposes tokens and messages | **VULNERABLE** — no TLS by default | HIGH |

### 4.5 Denial of Service

| # | Threat | Pre-fix status | Severity |
|---|--------|---------------|----------|
| D-1 | Socket event flooding (no global per-user rate limit) | **PARTIAL** — per-event limits exist, no aggregate cap | MEDIUM |
| D-2 | File upload floods disk (100 MB per file, no quota) | **VULNERABLE** — no per-user disk quota | MEDIUM |
| D-3 | Connection exhaustion (unlimited concurrent sockets) | **VULNERABLE** — no max-connections-per-user | MEDIUM |
| D-4 | Login brute-force (no lockout, no rate limiting on /auth/login) | **VULNERABLE** — configured but not applied | HIGH |

### 4.6 Elevation of Privilege

| # | Threat | Pre-fix status | Severity |
|---|--------|---------------|----------|
| E-1 | No role/permission model — all users have identical privileges | **DESIGN GAP** — no admin, moderator, or channel-owner roles | MEDIUM |
| E-2 | Call signaling relay allows signals when user has NO active call (logic bug) | **CRITICAL BUG** — `if call and target not in call.participants` passes when `call is None` | CRITICAL |
| E-3 | Race condition in `call_service.initiate_call` — no lock on check-then-act | **VULNERABLE** — asyncio.Lock missing | HIGH |

---

## 5. Attack Scenarios

### Scenario A: Token Theft via Physical Access
1. Attacker accesses victim's machine
2. Opens `%APPDATA%/CommClient/` → reads localStorage (LevelDB)
3. Extracts JWT tokens
4. Connects to server from another device
5. Full account takeover

**Mitigation:** Encrypt tokens at rest with machine-derived key.

### Scenario B: Unauthorized File Access
1. Attacker registers a legitimate account
2. Discovers file ID (via message content or enumeration)
3. Calls `GET /api/files/{file_id}` with own JWT
4. Downloads any file from any channel

**Mitigation:** Verify requester has access to the file's channel.

### Scenario C: Call Signaling Injection
1. Attacker is NOT in any call
2. Sends `signal_offer` with arbitrary `target_id`
3. Authorization check: `call = get_user_call(user_id)` → returns `None`
4. `if call and target_id not in call.participants` → `None and ...` → `False` → check passes
5. SDP offer relayed to victim

**Mitigation:** Fix logic to `if not call or target_id not in call.participants`.

### Scenario D: Login Brute-Force
1. Attacker enumerates usernames via registration error ("username taken")
2. Brute-forces passwords via `/api/auth/login` (no rate limiting)
3. Gets valid tokens

**Mitigation:** IP-based rate limiting on auth endpoints + account lockout after N failures.

---

## 6. Risk Ratings

| Risk | Likelihood | Impact | Rating |
|------|-----------|--------|--------|
| File download auth bypass (I-1) | HIGH | HIGH | **CRITICAL** |
| Signal relay auth bypass (E-2) | MEDIUM | HIGH | **CRITICAL** |
| JWT key hardcoded (S-4) | HIGH | CRITICAL | **CRITICAL** |
| Token plaintext storage (S-2) | MEDIUM | HIGH | **HIGH** |
| Login brute-force (D-4) | HIGH | MEDIUM | **HIGH** |
| No file channel auth on upload (I-1b) | HIGH | MEDIUM | **HIGH** |
| Call initiation race condition (E-3) | LOW | MEDIUM | **MEDIUM** |
| No user disk quota (D-2) | MEDIUM | LOW | **MEDIUM** |

---

## 7. Security Controls to Implement

### CRITICAL (P0)
1. Fix signal relay authorization logic bug
2. Add file download/upload channel membership authorization
3. Force random JWT secret on first run (persist to `.env`)
4. Add login rate limiting + account lockout

### HIGH (P1)
5. Add security middleware (headers, request size limits, CORS lockdown)
6. Add role/permission model (admin, member)
7. Fix call_service race condition with asyncio.Lock
8. Add token-to-IP binding (optional re-verify)
9. Encrypt tokens at rest in Electron (safeStorage / DPAPI)
10. Add CSP headers in Electron

### MEDIUM (P2)
11. Add MIME magic-byte validation for file uploads
12. Add per-user connection limits
13. Add global aggregate rate limiter
14. Add security audit logging
15. Sanitize error messages (no internal details)
16. Add Content-Disposition: attachment on file downloads
