# Helen / CommClient — API Reference

Helen-Server exposes ~350 REST endpoints across 50 route modules + a
Socket.IO event bus for real-time signaling. This document is the
operator-facing reference; for the canonical contract always read the
source — every endpoint is defined in
`CommClient-Server/app/api/routes/*.py`.

For a quickstart, see `DEPLOYMENT.md`. For implementation history, see
`WORK_LOG.md`.

---

## Conventions

- **Base URL** — `http://<host>:3000` for HTTP, `https://<host>:3443` for the TLS sidecar.
- **Auth** — most endpoints require `Authorization: Bearer <access_token>`. The token comes from `/api/auth/login` or `/api/auth/register`. Admin endpoints additionally require `role=admin` on the JWT.
- **Content-Type** — JSON for POST / PATCH bodies. File uploads are `multipart/form-data`.
- **Errors** — `{"detail": "<reason>"}` body with the HTTP status code reflecting the failure.
- **Rate limits** — every response includes `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Class`. A 429 response carries `Retry-After` (seconds). LAN/loopback traffic is allowlisted.

---

## Auth (`/api/auth`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/register` | Register a new user. First user is auto-promoted to `role=admin`. Returns `{user, tokens}`. |
| POST | `/api/auth/login` | Authenticate. Returns `{user, tokens}` with `access_token` (1h) + `refresh_token` (30d). |
| POST | `/api/auth/refresh` | Trade `refresh_token` for a fresh access token. |
| POST | `/api/auth/logout` | Revoke the supplied refresh token. |
| POST | `/api/auth/change-password` | Self-service password rotation; requires current password. |

bcrypt cost is 12 by default; the auth queue caps concurrent ops at `cpu_count/2` to keep the event loop healthy under stampedes.

---

## Users (`/api/users`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/users/me` | Self profile. |
| PATCH | `/api/users/me` | Update display name, bio, status message, etc. |
| GET | `/api/users` | List users (search by `?search=`, paginate by `?skip=&limit=`). |
| GET | `/api/users/{id}` | Profile by id. |
| GET | `/api/users/by-code/{code}` | Resolve a 64-char share_code → user. Falls back to federation when local lookup misses. |
| GET | `/api/users/me/photos` | List my profile-photo gallery. |
| POST | `/api/users/me/photos` | Upload a profile photo (`multipart`). |
| PATCH | `/api/users/me/photos/{id}` | Edit visibility / caption / primary flag. |
| DELETE | `/api/users/me/photos/{id}` | Delete one. |
| GET | `/api/users/me/contacts` | My contact list. |
| POST | `/api/users/me/contacts` | Add a contact. |
| DELETE | `/api/users/me/contacts/{id}` | Remove. |

---

## Channels (`/api/channels`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/channels` | List channels I'm a member of. |
| POST | `/api/channels` | Create a DM (`type=dm`) or group (`type=group`). |
| GET | `/api/channels/{id}` | Channel details + member list. |
| PATCH | `/api/channels/{id}` | Rename / update group metadata. |
| DELETE | `/api/channels/{id}` | Delete (creator/admin only; DM members can delete their DM). |
| POST | `/api/channels/{id}/members` | Add a member (admin/creator). |
| DELETE | `/api/channels/{id}/members/{user_id}` | Remove a member. |
| GET | `/api/channels/{id}/messages` | Paginated messages (`?before=&limit=`). |
| POST | `/api/channels/{id}/messages` | Send a message (alternative to socket emit). |
| GET | `/api/channels/{id}/unread` | My unread count for this channel. |
| GET | `/api/channels/{id}/read-states` | Per-member read positions. |
| GET | `/api/channels/{id}/active-call` | The live group call if one exists; drives "Join Existing Call" UI. |
| GET | `/api/channels/{id}/pins` | Pinned messages list. |

---

## Messages (`/api/messages`)

| Method | Path | Purpose |
|---|---|---|
| PATCH | `/api/messages/{id}` | Edit your own message. Body: `{content}`. |
| DELETE | `/api/messages/{id}` | Delete your own (or moderator's). |
| GET | `/api/messages/search` | Full-text search (`?q=&channel_id=`). |
| POST | `/api/messages/{id}/reactions` | Toggle reaction. Body: `{emoji}`. |
| GET | `/api/messages/{id}/receipts` | Per-user delivered/read state. |
| POST | `/api/messages/{id}/pin` | Pin (admin/creator). |
| DELETE | `/api/messages/{id}/pin` | Unpin. |
| POST | `/api/messages/{id}/forward` | Forward to another channel. Body: `{to_channel_id}`. |
| GET | `/api/messages/{id}/thread` | Thread replies. |

---

## Calls (`/api/calls`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/calls` | Call history. |
| DELETE | `/api/calls/{id}` | Hide a call from history. |
| DELETE | `/api/calls` | Clear all call history. |

Real-time call control happens over Socket.IO: `v2_call_initiate`, `v2_call_accept`, `v2_call_join_group`, `v2_call_leave_group`, `v2_call_hangup`, `v2_call_reject`, `v2_call_reinvite`, `call_signal`, `call_kick_participant`, `call_force_mute`, `call_end_for_everyone`, `auth_refresh`, etc. See `app/socket/call_handlers.py` for the full list.

---

## Files (`/api/files`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/files/upload` | Upload (`multipart`, optional `?channel_id=`). |
| GET | `/api/files/{id}` | Download. |
| GET | `/api/files/{id}/thumbnail` | Resized image preview. |
| POST | `/api/files/upload/begin` | Start a resumable session (`/api/files/upload/{session}/chunk`, `/api/files/upload/{session}/finalize`). |

Group-file BitTorrent-style swarm:

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/channels/{id}/group-file-offers` | Create an offer over an already-uploaded file. |
| GET | `/api/channels/{id}/group-file-offers` | List offers in channel. |
| GET | `/api/group-file-offers/inbox` | All offers I can accept. |
| POST | `/api/group-file-offers/{id}/accept` | Start receiving. |
| POST | `/api/group-file-offers/{id}/reject` | Decline. |
| POST | `/api/group-file-offers/{id}/chunks/{n}` | Report a chunk completed. |
| GET | `/api/group-file-offers/{id}/chunks/{n}/peers` | Find peers holding a chunk. |
| DELETE | `/api/group-file-offers/{id}` | Cancel (sender/admin). |

---

## ICE / TURN (`/api/turn`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/turn/ice-config` | Returns `{ice_servers, ice_transport_policy, ttl_seconds, realm}`. The desktop's `iceConfigService` caches this with TTL and refreshes when ≤120s remain. |

---

## Notifications (`/api/notifications`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/notifications` | List (`?limit=&offset=&unread_only=`). |
| GET | `/api/notifications/count` | Unread count. |
| POST | `/api/notifications/mark-read` | Mark a list of ids as read. |
| POST | `/api/notifications/mark-all-read` | Mark everything read. |
| DELETE | `/api/notifications/{id}` | Delete one. |
| POST | `/api/notifications/delete-all` | Wipe my list. |

---

## Health & discovery (no auth)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness — `{status, service, version}`. |
| GET | `/api/info` | Server profile — `{service, version, lan_ip, uptime_seconds, online_users}`. |
| GET | `/api/discovery` | Full broadcast payload — `{type, server_id, name, version, host, port, users_online, uptime, https_port, https_url, pair_url_https, ts}`. Federation-signed when `HELEN_DISCOVERY_SECRET` is set. |
| GET | `/api/cluster/info` | This node's role + capacity + load snapshot. |
| GET | `/api/cluster/members` | All known cluster members. |
| GET | `/api/connection/diagnostics` | Per-client connection report (server reachable / auth valid / user online / socket+session counts). |

---

## Admin (`/api/admin/*`) — `role=admin` required

### Stats / monitoring
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/admin/stats` | KPIs (users, channels, messages, system metrics). |
| GET | `/api/admin/active-calls` | Live call list. |
| GET | `/api/admin/connected-clients` | One row per live Socket.IO connection. |
| GET | `/api/admin/audit-logs` | Filterable audit trail (`?event=&user_id=&success=&limit=`). |
| GET | `/api/admin/audit-logs/events` | Distinct event names for the filter dropdown. |
| GET | `/api/admin/diagnostics/network` | UDP listener / mDNS / public IP / NIC info. |
| GET | `/api/admin/sfu/status` | SFU launcher snapshot + live `/healthz` probe. |

### User management
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/admin/kick/{user_id}` | Force-disconnect every socket. |
| POST | `/api/admin/ban/{user_id}` | Soft-ban (`is_active=False`). |
| POST | `/api/admin/unban/{user_id}` | Reverse a ban. |
| POST | `/api/admin/set-role/{user_id}` | Promote/demote. Body: `{role}`. |
| POST | `/api/admin/reset-password/{user_id}` | Operator-driven password reset. |
| GET | `/api/admin/users/{user_id}/sessions` | This user's active sessions. |
| DELETE | `/api/admin/users/{user_id}/sessions/{session_id}` | Revoke one. |
| POST | `/api/admin/users/{user_id}/sessions/revoke-all` | Revoke every session. |

### Backups
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/admin/backups` | List. |
| POST | `/api/admin/backups` | Create. |
| POST | `/api/admin/backups/run-now` | Trigger the scheduled job. |
| GET | `/api/admin/backups/scheduler` | Cadence + last-run snapshot. |
| POST | `/api/admin/backups/{name}/restore` | Restore (server restarts). |
| POST | `/api/admin/backups/{name}/verify` | Integrity check. |
| GET | `/api/admin/backups/{name}/download` | Stream the file. |
| DELETE | `/api/admin/backups/{name}` | Delete. |

### Federation + connectivity
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/admin/federation/status` | Enabled / peer counts / last gossip. |
| GET | `/api/admin/federation/metrics` | Counters. |
| GET | `/api/admin/federation/events` | Recent federation events (`?limit=`). |
| GET | `/api/admin/federation/bridges` | Active relay bridges. |
| GET | `/api/admin/federation/topology` | Graph snapshot. |
| POST | `/api/admin/federation/generate-secret` | Mint a new HMAC secret. |
| GET | `/api/admin/connectivity` | Reverse-tunnel + relay state. |
| POST | `/api/admin/connectivity/tunnel` | Configure tunnel. Body: `{ws_url, token, display_name?}`. |
| DELETE | `/api/admin/connectivity/tunnel` | Tear down. |
| POST | `/api/admin/connectivity/router/apply` | UPnP / NAT-PMP apply. |

### DLQ + cleanup
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/admin/dlq` | List dead-letter entries (`?status_filter=&kind_filter=&limit=`). |
| GET | `/api/admin/dlq/stats` | Aggregate counts. |
| POST | `/api/admin/dlq/{entry_id}/replay` | Re-publish. |
| POST | `/api/admin/cleanup/sessions` | Drop expired JWT rows. |
| POST | `/api/admin/cleanup/files` | Drop orphan uploads. |

### Server config / roles / control plane
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/admin/server-config` | Runtime config snapshot. |
| PATCH | `/api/admin/server-config` | Update server name + tunables. |
| GET | `/api/admin/server-roles` | Per-channel role caps. |
| PATCH | `/api/admin/server-roles` | Update. |
| GET | `/api/admin/control-plane/status` | Phase + profile + last decisions. |
| GET | `/api/admin/control-plane/decisions` | Decision log. |
| POST | `/api/admin/control-plane/profile` | Switch profile. Body: `{profile}`. |
| POST | `/api/admin/control-plane/emergency/exit` | Force-exit emergency mode. |
| GET | `/api/admin/control-plane/rooms` | Room-level placement. |
| POST | `/api/admin/control-plane/rooms/{id}/force` | Pin to mode. |
| GET | `/api/admin/placement/nodes` | Cluster placement view. |
| GET | `/api/admin/placement/capacity` | Capacity caps. |
| PATCH | `/api/admin/placement/capacity` | Adjust. |

### Peer approval (`/api/admin/peers/*`)
Five buckets + six actions per peer:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/admin/peers/discovered` | Newly seen peers. |
| GET | `/api/admin/peers/pending` | Waiting for admin review. |
| GET | `/api/admin/peers/approved` | Trusted peers. |
| GET | `/api/admin/peers/rejected` | Rejected. |
| GET | `/api/admin/peers/denied` | Hard-blocked. |
| POST | `/api/admin/peers/{server_id}/approve` | Promote to APPROVED → READY. |
| POST | `/api/admin/peers/{server_id}/reject` | Body: `{reason}`. |
| POST | `/api/admin/peers/{server_id}/deny` | Permanent block. Body: `{reason}`. |
| POST | `/api/admin/peers/{server_id}/ignore` | Drop without state change. |
| POST | `/api/admin/peers/{server_id}/trust-permanently` | Skip future re-approval. |
| POST | `/api/admin/peers/{server_id}/trust-once` | One-shot trust. |

---

## Federation (`/api/federation/*`) — HMAC-signed peer-to-peer

All requests require `X-Federation-Origin`, `X-Federation-Timestamp`, `X-Federation-Signature` headers (HMAC-SHA256 over `(ts|method|path|body)` with `FEDERATION_SECRET`). Idempotent retries (same sig+ts) are allowed; replays with different sigs are refused.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/federation/peer-announce` | Discovery handshake. Always fail-OPEN at the gate. |
| GET | `/api/federation/peer-probe/{id}` | Liveness check. |
| GET | `/api/federation/users/by-code/{code}` | Cross-server share-code lookup. Seeds `federated_presence`. |
| POST | `/api/federation/presence` | Push online/offline updates. |
| POST | `/api/federation/emit` | Generic event fan-out. |
| POST | `/api/federation/call/rpc` | Forward call lifecycle (accept/reject/leave/hangup/reinvite/heartbeat/join). |
| GET | `/api/federation/files/{id}/locate` | Does this peer host this file? |
| GET | `/api/federation/files/{id}/content` | Range-aware byte stream (re-auths the requester via `X-Federation-Acting-User`). |
| POST | `/api/federation/dht/find_node` | Kademlia find. |
| POST | `/api/federation/gossip/peers` | Periodic peer-list gossip. |
| GET | `/api/federation/presence/snapshot` | Bulk presence sync. |

The HTTP gate uses **TRANSIENT_PEER_STATES** (DISCOVERED → SYNCING_STATE) as fail-open + ACTIVE_PEER_STATES as allow + WAITING/REFUSED as block. See `tests/test_federation_gate.py`.

---

## Metrics (`/api/metrics`)

Prometheus exposition. Three auth modes (token / admin role / `HELEN_METRICS_PUBLIC=1` in non-prod). Returns 6+ metric families:

```
helen_route_executor_events_total{outcome=...}
helen_ack_events_total{outcome=...}
helen_ack_in_flight
helen_broker_events_total{outcome=...}
helen_priority_queue_depth{priority=P0..P4}
helen_priority_queue_events_total{priority,outcome}
helen_load_cpu_percent / _memory_percent / _event_loop_lag_ms / _active_sockets / _active_calls / _health_score
helen_fabric_subscriber_events_total{outcome=...}
helen_trace_events_total{outcome=...}
helen_bcrypt_max_parallel / _in_flight / _waiting
helen_active_sockets_total / _active_users_total
helen_peer_state_count{state=...}
```

---

## Chaos & misc

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/chaos/inject_failure` | Force a target to fail (admin + `HELEN_ENABLE_100_HOP_TEST_MODE=true`). |
| DELETE | `/api/chaos/inject_failure/{target}` | Clear. |
| POST | `/api/chaos/inject_congestion` | Override load metrics for a server_id. |
| POST | `/api/chaos/force_route` | Pin a trace_id to a specific route. |
| GET | `/api/chaos/state` | Current injections. |
| GET | `/api/chaos/traces` | Inspect recent route traces. |
| GET | `/api/chaos/traces/{trace_id}` | Full hop chain. |

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/pair/request` | Mint a phone-pair token (QR-encoded URL). |
| GET | `/api/pair/sessions` | My active phone pair sessions. |
| DELETE | `/api/pair/sessions/{phone_sid}` | Force-disconnect a paired phone. |
| POST | `/api/sessions/revoke-all` | Revoke all my JWT sessions. |
| GET | `/api/uplink` | Reverse-tunnel state. |

---

## Socket.IO events

Emit / listen on the same connection that auth'd via the JWT. Highlights:

### Lifecycle
- `auth_refresh` — trade refresh_token for a fresh access_token without reconnect.
- `presence:user_status` — broadcast on connect/disconnect.
- `presence:status_message_changed` — custom status updates.

### Chat
- `v2_chat_send_message` (server-side handler in sync_handlers.py).
- `v2_chat:new_message` — broadcast on send (carries `seq` for gap detection).
- `v2_chat:message_delivered` / `:message_read`.
- `v2_chat_subscribe_channel`, `v2_chat_typing_start/stop`.
- `chat:typing` — broadcast variant for federated mesh.

### Calls (v2)
- `v2_call_initiate`, `v2_call_accept`, `v2_call_reject`, `v2_call_hangup`, `v2_call_reinvite`.
- `v2_call_join_group`, `v2_call_leave_group`.
- `v2_call_toggle_mute`, `v2_call_toggle_video`.
- `call_kick_participant`, `call_force_mute`, `call_end_for_everyone` (host/moderator).
- `call_signal` (unified offer/answer/ice — replaces v1 signal:offer/answer/ice_candidate).
- `call_incoming`, `call_accepted`, `call_rejected`, `call_hangup`.
- `call_participant_joined`, `call_participant_left`, `call_participant_state`.
- `call:host-changed`, `call:force_muted`, `call:kicked`, `call:missed`.
- `call:active_call_started`, `call:active_call_ended` (drives "Join Existing Call" UI).
- `call_topology_updated` (mesh ↔ SFU switch).

### File drop
- `file_drop:offer`, `:accepted`, `:rejected`, `:cancelled`.

### Group files
- `group_file:offer_created`, `:offer_accepted`, `:chunk_available`, `:offer_completed`.

### Notifications
- `notification:new`.

### Whiteboard
- `whiteboard:stroke`, `:cursor`, `:join`, `:leave`, `:participants`.

### E2EE
- `e2ee:session_request`, `:session_ack`. (Currently gated as broken — see store.)

---

## Static mounts

| Path | Purpose |
|---|---|
| `/admin/` | Browser admin dashboard (Arabic RTL, 3565 lines). |
| `/admin-mobile/` | iOS-Admin web simulator. |
| `/mobile/` | iOS client web simulator. |
| `/admin-secret/` | Master-code-gated emergency admin. |
| `/vault/` | Vault static panel. |
| `/hub/` | Hub multi-app launcher. |

---

## TypeScript client

The Electron renderer wraps every endpoint in `src/renderer/services/api.client.ts`:

- `api.register / login / logout / changePassword / refreshTokens`
- `api.getMe / updateMe / listUsers / lookupByCode / listPeers / getServerIdentity`
- `api.createChannel / listChannels / addMember / removeMember`
- `api.getMessages / sendMessage / editMessage / deleteMessage / pinMessage / forwardMessage`
- `api.toggleReaction / searchMessages`
- `api.uploadFile / getFileUrl / getThumbnailUrl`
- `api.getCallHistory / getChannelActiveCall`
- `api.iceConfig / health / uplink / info`
- `api.groupFileOffers.{create, listChannel, inbox, accept, reject, cancel, ...}`
- `api.requestPairToken / listPairSessions / terminatePairSession`
- `api.admin.*` — 50+ admin methods (stats, users, audit, dlq, backups, federation, connectivity, server-config, control-plane, placement, sfuStatus, cleanup).
- `api.adminPeers.*` — peer approval (discovered/pending/approved/rejected/denied + approve/reject/deny/ignore/trust-permanently/trust-once).

429 responses are honoured once with `Retry-After` before the error surfaces to the caller.
