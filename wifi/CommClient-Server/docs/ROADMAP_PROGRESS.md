# CommClient — Roadmap Progress

_Last updated: 2026-04-18_

This is the living ledger of everything that has shipped into the
`CommClient-Server` backend (plus the server-facing work in
`CommClient-Desktop` and `sfu-worker`). Each section describes **what**
landed, **where** it lives, and **how to operate / extend** it. The
ordering mirrors the task numbers in our internal TODO list so future
work can cite a section directly.

> CommClient is a LAN-only communications platform. The backend is
> FastAPI + Socket.IO + SQLAlchemy async over aiosqlite; the SFU is a
> mediasoup worker; the desktop client is Electron + React. All three
> have been hardened toward production-grade reliability.

---

## Table of contents

1. [Durability: SQLite WAL + migrations](#1-durability-sqlite-wal--migrations)
2. [Real-time call plane](#2-real-time-call-plane)
3. [Resumable file uploads](#3-resumable-file-uploads)
4. [Call topology: mesh ↔ SFU auto-switch](#4-call-topology-mesh--sfu-auto-switch)
5. [mediasoup SFU worker](#5-mediasoup-sfu-worker)
6. [Security hardening (uploads, rate-limit, quotas)](#6-security-hardening)
7. [Per-recipient file acceptance](#7-per-recipient-file-acceptance)
8. [SFU enhancements (BWE, speaker, recording, TURN)](#8-sfu-enhancements)
9. [@mentions notification dispatch](#9-mentions-notification-dispatch)
10. [Messaging dead-letter queue (DLQ)](#10-messaging-dead-letter-queue)
11. [Group-file multicast (P2P swarm)](#11-group-file-multicast)
12. [Repository layout & lifecycle wiring](#12-repository-layout--lifecycle-wiring)
13. [Multi-worker leader election](#13-multi-worker-leader-election)

---

## 1. Durability: SQLite WAL + migrations

Tasks: **#1, #5, #15**

**Why**: the server needs crash-safe durability without giving up
SQLite's single-binary deployment footprint.

* `app/db/session.py` configures SQLite with WAL journaling,
  `synchronous=NORMAL`, `busy_timeout=30_000`, a 128 MB page cache,
  memory temp store, 256 MB `mmap_size`, and `foreign_keys=ON`.
* Alembic migrations are stored under `migrations/versions`. Startup
  runs `alembic upgrade head` programmatically so a blank data dir
  comes online with the latest schema. The latest revisions are:
  * `004` — per-recipient file acceptance, message-dead-letter, and
    related indexes.
  * `005` — `group_file_offers` + `group_file_chunk_availability`.
* Backups: `app/services/backup_service.py` snapshots the DB on
  startup and on a configurable cadence. Files are rotated per the
  config in `app/core/config.py`.

**Operating notes**

* To add a new migration: `alembic revision -m "<title>"` → edit the
  generated file → `alembic upgrade head`. The server applies pending
  migrations automatically on next startup.
* Never drop columns in a live deploy — SQLite can't drop columns in
  place before 3.35; use an additive column + code-level shim.

---

## 2. Real-time call plane

Tasks: **#2, #10, #11, #12, #19, #20, #21, #22, #23**

**Why**: calls were originally in-memory only, losing state on
reconnects and orphaning mediasoup routers.

* Persisted entities: `ActiveCall`, `ActiveCallParticipant`, and
  `CallSignalEvent` (see `app/models/active_call.py`). Every start /
  join / leave / end transitions a row, so a cold reboot can rehydrate
  live calls.
* Client-side: the Electron renderer runs a `call_heartbeat` loop
  every ~10 s keyed by `(call_id, participant_id)`. Missed beats
  beyond the grace window flip the participant to `left`.
* Server: `chat_handlers` / `call_handlers` atomically update the call
  row + in-memory map behind a per-call async lock. The orphan sweep
  (`sync_in_memory_state`) reconciles DB ↔ memory at startup.
* SFU router lifecycle: closing the last participant tears down the
  mediasoup router; on any error path we always call
  `MediasoupSFUAdapter.release(call_id)`.
* Replay truncation: on reconnect, the client requests signals after
  its last-seen event id; if the server has truncated, it returns
  `truncated=true` and the client performs a full renegotiate.

---

## 3. Resumable file uploads

Tasks: **#4, #7, #13, #14, #18**

**Why**: large LAN transfers need chunked, resumable uploads with
per-chunk integrity and mid-flight auth recovery.

* Server module: `app/services/resumable_upload_service.py` +
  `app/api/routes/files_resumable.py`. Endpoints:
  * `POST /files/resumable/init` → issues `upload_id`, chunk manifest.
  * `PUT /files/resumable/{upload_id}/{chunk_index}` → accepts a chunk
    with CRC32 (fast) + SHA-256 (final verification) checksums.
  * `POST /files/resumable/{upload_id}/complete` → re-assembles and
    flips the `UploadSession` to `completed`.
* Client: `CommClient-Desktop/src/services/ResumableUploader.ts`
  drives the protocol, handles 401 → token refresh → retry, resumes
  from the server's reported `received_chunks` after a reconnect.
* `FileDropManager` was migrated to use `ResumableUploader` so all
  large channel/DM file sends benefit from the same retry semantics.

**Limits**: chunk size `[16 KiB, 4 MiB]`, total size capped by
`FILE_MAX_BYTES` in config. Abnormal chunk size or index is rejected
at the HTTP layer before hitting disk.

---

## 4. Call topology: mesh ↔ SFU auto-switch

Tasks: **#3, #6, #8, #16, #17**

`app/services/topology_coordinator.py` observes active-call state
(participant count, jitter / RTT on transport snapshots) and
transitions `mesh` ↔ `sfu` topologies when the cost curve flips.
Clients are notified via the `topology:switch` Socket.IO event, which
`TopologyCoordinator` on the renderer consumes to swap peer
connections.

* **Mesh**: every participant is a peer of every other via
  `RTCPeerConnection` objects managed in `GroupCallManager`.
* **SFU**: `MediasoupSFUAdapter` owns a single transport per peer with
  a router on the SFU worker.
* **Fallback**: if renegotiation fails, client falls back to a full
  SDP re-negotiation path (`TopologyCoordinator.fullRenegotiate`).

Call signal replay on reconnect is truncation-aware — the client
requests signals by last-seen id and either replays or restarts.

---

## 5. mediasoup SFU worker

Tasks: **#24, #25, #26, #9**

`CommClient-Server/sfu-worker/` — a Node.js process that hosts
mediasoup routers.

* `index.js` exposes a UDP/TCP control channel to the Python server
  (`app/api/routes/sfu_events.py`) so router-create / transport-
  connect / producer-paused events round-trip.
* The Python side (`app/services/sfu_service.py`) treats the worker
  as a remote backend; each call gets one router + one pipeline.
* Worker tears down routers on call end and on process signals.

Server/client event contract is documented in
`CommClient-Server/sfu-worker/CONTRACT.md`.

---

## 6. Security hardening

Tasks: **#27, #28**

* **Magic-byte MIME validation** — uploads now check the first 8 KiB
  against a registry of signatures (`app/core/security_utils.py ::
  detect_mime_from_magic_bytes`). Declared MIME must match detected
  MIME or the upload is rejected with `415`.
* **Upload rate limiting** — `app/socket/rate_limiter.py` tracks
  per-user token buckets for HTTP upload endpoints. Default: 60 req/
  min, 2 GiB/hour. Quotas tunable via env vars.
* **Auth** — all mutating endpoints depend on `get_current_user_id`
  (JWT access-token bearer). Refresh tokens rotate on exchange; old
  refresh hashes are blacklisted.

---

## 7. Per-recipient file acceptance

Task **#29**. Migration `004` + `app/models/file_acceptance.py`.

Separates "did the file land on this user's device" from "did the user
accept it". Each `FileAcceptance` row tracks
`pending → delivered → accepted|rejected` with `bytes_received`.
REST: `/files/{file_id}/acceptance`. Inbox router lists pending
acceptances so the desktop app can render a "waiting on you" badge.

---

## 8. SFU enhancements

Tasks: **#30, #31, #32, #33**

* **BWE / producer pause**: when the worker's transport reports
  congestion, we pause the lowest-priority producers and resume them
  once bandwidth recovers. Implemented in the worker
  (`sfu-worker/src/pipeline.js`) with a Python mirror that emits
  `call:producer_paused` socket events.
* **Active speaker detection**: mediasoup `AudioLevelObserver` feeds
  `active_speaker` events at ~4 Hz. Clients pin the active speaker in
  the main tile.
* **Call recording**: `PlainRtpTransport` pipe to ffmpeg writing WAV/
  WEBM segments under `data/recordings/<call_id>/`. Metadata in
  `CallLog`.
* **TURN fallback**: `app/services/turn_service.py` provisions
  short-lived credentials; `TopologyCoordinator` auto-enables TURN
  when direct candidates fail on the client.

---

## 9. @mentions notification dispatch

Task **#34**. See `app/services/message_service.py ::
dispatch_mentions`.

* `@username` tokens are extracted on message send with a strict
  regex (see `app/core/mentions.py`).
* Each mentioned user gets:
  1. A `Notification` row.
  2. A push notification via the `PushDispatcher` (FCM/APNs).
  3. A real-time `notification:new` Socket.IO emit if connected.
* `@everyone` / `@channel` fan out to all channel members subject to
  role checks on the sender.

Scheduled messages re-invoke `dispatch_mentions` when they fire so
offline-scheduled pings are not swallowed.

---

## 10. Messaging dead-letter queue

Task **#35**. Service: `app/services/dead_letter_service.py`. Model:
`app/models/message_dead_letter.py`. Admin REST:
`app/api/routes/dlq.py`.

**What it captures**

| `kind`     | Source                                    | Trigger                       |
|------------|-------------------------------------------|-------------------------------|
| `fanout`   | `chat_handlers` / `sync_handlers`         | per-member emit failure / top-level handler exception |
| `webhook`  | `webhook_service`                         | delivery exhausts `MAX_ATTEMPTS` |
| `push`     | `push_dispatcher`                         | `sent == 0 and failed > 0`    |
| `scheduled`| `scheduled_message_service`               | attempt_count reaches `MAX_ATTEMPTS` |

Each row stores the original payload, the error, a monotonically
increasing `attempt_count`, a computed `next_attempt_at` (exponential
backoff capped at `MAX_BACKOFF_SECONDS = 3600`), and a final
`replayed | abandoned | pending | replaying` status.

**Reaper**

`DeadLetterService.start()` / `stop()` are wired into `app/main.py`
lifespan. The reaper scans for rows whose `next_attempt_at` is due and
dispatches them via per-kind handlers.

**Admin surface**

`/api/admin/dlq` (all `require_role("admin")` + audit logged):

* `GET /` — list with status/kind filters + pagination.
* `GET /stats` — counts grouped by status / kind.
* `GET /{id}` — detail incl. payload.
* `POST /{id}/replay` — manual retry.
* `POST /{id}/abandon` — mark abandoned with note.
* `POST /reaper/tick` — force-run one cycle.
* `POST /purge-replayed` — housekeeping.

Tests: `tests/test_dead_letter_service.py` — 17 tests, all passing.

---

## 11. Group-file multicast

Task **#36**. Migration `005`. Model, service, REST, socket, tests —
all wired.

### Problem

A single sender dropping a 500 MiB file into a 30-person channel would
saturate their uplink while the server fanned the same bytes to every
receiver. We want:

1. The sender uploads **once** (existing resumable upload flow).
2. The server offers it to every channel member.
3. Recipients can pull chunks from either the server or from a peer
   that already has that chunk — BitTorrent-style — to flatten the
   sender's uplink.

### Data model (migration `005`)

`group_file_offers`
  : one row per logical multicast. Columns: `sender_id`,
    `channel_id`, `file_id`, denormalised file metadata
    (`filename`, `file_size`, `mime_type`, `chunk_size`,
    `total_chunks`, `checksum`, `caption`), `status`
    (`offered|active|completed|cancelled|expired`), `swarm_enabled`,
    aggregate counters (`accepted_count`, `rejected_count`,
    `completed_count`, `expected_recipients`), `expires_at`,
    timestamps. Indexes on `(channel_id, status)` and
    `(status, expires_at)`.

`group_file_chunk_availability`
  : composite PK `(offer_id, user_id)`. Per-recipient lifecycle
    (`pending|accepted|completed|declined|abandoned`) plus a packed
    **chunk bitmap**: 8 chunks per byte, LSB first — chunk _i_ → bit
    (_i_ mod 8) of byte (_i_ div 8). `NULL` means "no chunks yet".
    Counters `chunks_received`, `bytes_received`,
    `last_progress_at`, `completed_at`.

### Service (`app/services/group_file_service.py`)

| Method                        | Semantics                                                                          |
|-------------------------------|------------------------------------------------------------------------------------|
| `create_offer`                | membership + chunk-size validation, fan-out availability rows for every non-sender |
| `accept_offer`                | `pending → accepted`, bumps offer counters, idempotent                             |
| `reject_offer`                | `pending → declined`, may close the offer if all recipients have responded         |
| `report_chunk_received`       | sets bit in bitmap, auto-promotes from `pending → accepted`, flags `became_complete` when last chunk lands |
| `get_chunk_peers`             | returns peers whose bitmap has chunk _N_; always includes sender as an implicit source; honours `swarm_enabled=False` |
| `cancel_offer`                | sender/admin can cancel; abandons all active recipients                            |
| `sweep_expired`               | flips `expires_at`-past offers to `expired`, abandons recipients                   |
| `cleanup_stale_recipients`    | abandons `accepted` recipients idle longer than `STALE_RECIPIENT_GRACE` (6 h)      |
| `get_offer_stats`             | dashboard aggregate — recipients by status                                         |

Guardrails:
* `MIN_CHUNK_SIZE = 64 KiB`, `MAX_CHUNK_SIZE = 64 MiB`.
* `MAX_TOTAL_CHUNKS = 1_048_576` (→ 128 KiB bitmap per peer at worst).
* `MAX_OFFER_TTL = 7 days`; default TTL 24 h.

### REST (`/api/group-file-offers`)

| Method | Path                                                             | Purpose                |
|--------|------------------------------------------------------------------|------------------------|
| POST   | `/channels/{channel_id}/group-file-offers`                       | create                 |
| GET    | `/channels/{channel_id}/group-file-offers?status=`               | list per channel       |
| GET    | `/group-file-offers/inbox`                                       | my incoming            |
| GET    | `/group-file-offers/{offer_id}`                                  | detail                 |
| GET    | `/group-file-offers/{offer_id}/stats`                            | dashboard (sender/admin) |
| POST   | `/group-file-offers/{offer_id}/accept`                           | recipient action       |
| POST   | `/group-file-offers/{offer_id}/reject`                           | recipient action       |
| POST   | `/group-file-offers/{offer_id}/chunks/{chunk_index}`             | report chunk landed    |
| GET    | `/group-file-offers/{offer_id}/chunks/{chunk_index}/peers`       | swarm lookup           |
| DELETE | `/group-file-offers/{offer_id}`                                  | cancel (sender/admin)  |
| POST   | `/group-file-offers/_sweep-expired`                              | admin / cron           |

### Socket.IO (`app/socket/group_file_handlers.py`)

Client → server: `file_drop:group_offer`,
`file_drop:group_accept`, `file_drop:group_reject`,
`file_drop:group_chunk_received`, `file_drop:group_chunk_peers`,
`file_drop:group_cancel`.

Server → clients: `file_drop:group_offer_created`,
`file_drop:group_offer_updated`, `file_drop:group_peer_available`
(broadcast on first-time chunk arrival — this is the swarm signal),
`file_drop:group_offer_completed`, `file_drop:group_offer_ack`,
`file_drop:group_offer_error`.

All handlers isolate exceptions per-event and dispatch via
`async_session_factory` — a misbehaving client cannot kill the event
loop.

### Lifecycle wiring

`app/main.py` lifespan starts a background sweeper
(`_group_file_sweep_loop`) that calls `sweep_expired` +
`cleanup_stale_recipients` every 120 s. It is cancelled on shutdown
with a 3 s grace timeout.

### Tests

`tests/test_group_file_service.py` — **15 tests, all passing**:

* Bitmap helpers (`set_chunk`, `has_chunk`, `held_chunk_indexes`,
  `is_complete`, out-of-range rejection, empty state).
* Offer creation — excludes sender, populates availability rows,
  rejects non-members, validates chunk size and total chunks.
* Accept flow — promotes offer to `active`, idempotent, blocks a
  subsequent accept after reject.
* Chunk reporting — auto-promotes pending → accepted, first report
  flips bit, re-report no-ops, last chunk triggers `became_complete`
  + `status = completed`.
* Swarm lookup — includes sender + peers holding the chunk, respects
  `swarm_enabled=False` (only sender).
* Cancel — flips `pending/accepted` rows to `abandoned`.
* Sweep expired — past `expires_at` offers move to `expired`.

### Client integration (TODO)

The desktop client has not yet been wired to the new events; follow-up
work will extend `FileDropManager.ts` to:
1. Emit `file_drop:group_offer` after a successful resumable upload
   when the message is channel-scoped and the channel has ≥ 3 members.
2. Subscribe to `file_drop:group_peer_available` and prefer peer
   sources over server pulls.
3. POST to `/chunks/{idx}` on successful chunk fetch so the server's
   bitmap stays in lockstep.

---

## 12. Repository layout & lifecycle wiring

### Directories

```
CommClient-Server/
├── app/
│   ├── api/routes/                 # REST routers — one file per domain
│   ├── core/                       # config, logging, deps, audit, exceptions
│   ├── db/                         # Base, session, engine
│   ├── models/                     # SQLAlchemy models (one file per aggregate)
│   ├── services/                   # business logic, one class per domain
│   ├── socket/                     # Socket.IO handlers (one file per domain)
│   └── transports/                 # LAN transport discovery / bridges
├── migrations/versions/            # Alembic revisions (001..005)
├── sfu-worker/                     # Node.js mediasoup worker
├── tests/                          # pytest-asyncio
├── docs/                           # <you are here>
└── data/                           # sqlite + WAL + backups + uploads
```

### Lifespan (startup → shutdown)

`app/main.py :: lifespan` — startup order:

1. `engine.begin()` → run Alembic migrations to head.
2. `BackupService.start()` — snapshot cadence.
3. `UdpBroadcastService`, `MdnsService`, `UdpListenerService`.
4. `AuditWriter.start()` — background flush of audit log.
5. Singleton loops wrapped in `run_as_leader` / `run_supervised_as_leader`:
   call-orphan sweeper (45 s), WAL checkpoint (600 s, SQLite only),
   upload GC (300 s), status-message expiry (60 s), channel-mute
   expiry (60 s), scheduled-message dispatcher, webhook dispatcher,
   poll expiry (60 s), DLQ reaper, group-file sweeper (120 s).
6. Heartbeat-cleanup loop (NOT leader-gated — per-worker sid map).

See §13 for the leader-election mechanism.

Shutdown is the reverse: notify clients via `server:shutdown`, stop
audit writer, signal webhook `stop_event`, cancel every leader-gated
task uniformly (each releases its lease in `finally`), cancel
heartbeat cleanup, UDP/mDNS services, then dispose the engine.

### Known ops hazards

* **SQLite on network filesystems** — do not mount the `data/`
  directory on NFS / SMB. WAL mode requires functional `fcntl` locks.
* **Multi-worker Python** — supported via `app.services.leader_election`
  (see §13). Default backend `single` (dev/SQLite) makes every worker
  leader with zero I/O. For Postgres set `LEADER_ELECTION_BACKEND=postgres`
  and every singleton loop (DLQ reaper, group-file sweeper, scheduled-
  message dispatcher, webhook dispatcher, call-orphan sweeper, poll
  expiry, channel-mute expiry, status-message expiry) is gated behind
  `pg_try_advisory_lock`. For shared Redis: `LEADER_ELECTION_BACKEND=redis`
  + `REDIS_URL=redis://...` (SET NX PX + Lua refresh/release).
* **Test DB path** — CI sets `SQLITE_PATH=/tmp/...` because the mounted
  workspace does not support SQLite WAL locks reliably.

---

## 13. Multi-worker leader election

Task: **#41**

**Why**: several background loops mutate global state and MUST run on
exactly one worker. Running them on every worker is either wasteful
(sweeper fan-out that re-does the same work) or catastrophic — the
scheduled-message dispatcher and webhook dispatcher would double-fire
side-effects until the `_claim_due` optimistic updates serialize them.

### Module: `app/services/leader_election.py`

Pluggable `LeaderElectionBackend` ABC with three implementations:

| Backend            | Mechanism                                                                                       | When to use                                 |
|--------------------|-------------------------------------------------------------------------------------------------|---------------------------------------------|
| `single`           | Always-leader no-op. Zero I/O.                                                                  | Default for SQLite / dev / single-worker.   |
| `postgres`         | `pg_try_advisory_lock(bigint_key)` on a dedicated per-lock connection. Lock auto-released on session drop. | `DB_BACKEND=postgresql`, any worker count. |
| `redis`            | `SET key owner NX PX ttl_ms` + Lua atomic PEXPIRE refresh + Lua atomic owner-checked DEL.       | Polyglot deployment with shared Redis.      |

Worker identity is `{hostname}:{pid}:{uuid8}` — stable for the process
lifetime. The Postgres advisory bigint key is derived via
`blake2b(name, digest_size=8)` collapsed to signed 64-bit.

### Public API

```python
from app.services.leader_election import (
    try_acquire, heartbeat, release,
    run_as_leader, run_supervised_as_leader,
    LeaderLoopConfig, DEFAULT_LEASE_TTL,
)

# Pattern A — one-shot ticks (sweepers/expiry loops):
await run_as_leader(LeaderLoopConfig(
    name="group_file_sweeper",
    interval=120.0,
    fn=_group_file_sweep_tick,
    ttl_seconds=60,
    initial_delay=30.0,
    jitter=0.1,
))

# Pattern B — services that own their own internal loop:
await run_supervised_as_leader(
    "scheduled_message_dispatcher",
    lambda: ScheduledMessageService.run_dispatch_loop(),
    ttl_seconds=60,
    initial_delay=5.0,
)
```

`run_as_leader` owns the whole loop: acquires the lease, heartbeats at
50 % of the TTL, invokes `fn` while leader, jittered sleep between
ticks, releases on `CancelledError`.

`run_supervised_as_leader` is the wrapper for pre-existing services
(`ScheduledMessageService`, `WebhookService`, `DeadLetterService`) —
it calls `factory()` to create a fresh inner task on acquisition,
cancels it when the lease is lost, restarts it if it exits while we
remain leader, and always releases the lease in `finally`.

### Configuration

`app/core/config.py`:

```python
LEADER_ELECTION_BACKEND: str | None = None  # single | postgres | redis
REDIS_URL: str | None = None
LEADER_LEASE_TTL_SECONDS: int = 60
```

If `LEADER_ELECTION_BACKEND` is unset we auto-detect: Postgres
backend when `DB_BACKEND=postgresql`, else single-process. Unknown
backend values or a missing `REDIS_URL` fall back safely to
`_SingleProcessBackend` with a warning.

### Gated loops

The following loops in `app/main.py :: lifespan` are wrapped:

| Loop                              | Interval | Pattern    | TTL reason                                      |
|-----------------------------------|----------|------------|-------------------------------------------------|
| `call_orphan_sweeper`             | 45 s     | one-shot   | Reaps ended calls across workers; idempotent-safe. |
| `wal_checkpoint` (SQLite only)    | 600 s    | one-shot   | Uniform pattern; WAL is process-local.          |
| `upload_gc`                       | 300 s    | one-shot   | Staging-dir GC; duplicates do wasted rm-rf.     |
| `status_message_expiry`           | 60 s     | one-shot   | Single UPDATE is fine duplicated but wasteful.  |
| `channel_mute_expiry`             | 60 s     | one-shot   | Same.                                            |
| `poll_expiry`                     | 60 s     | one-shot   | Same.                                            |
| `group_file_sweeper`              | 120 s    | one-shot   | Stale-recipient cleanup; fan-out wastes DB IO.  |
| `scheduled_message_dispatcher`    | 15 s     | supervised | **CRITICAL** — double-dispatch delivers msgs N×.|
| `webhook_dispatcher`              | 10 s     | supervised | **CRITICAL** — webhooks amplify downstream.     |
| `dlq_reaper`                      | internal | supervised | **CRITICAL** — reaping is not idempotent.       |

The heartbeat cleanup loop (`_heartbeat_cleanup_loop`) is intentionally
NOT gated — it only `sio.disconnect()`s sids attached to *this* worker,
so gating it would strand stale sockets on non-leader workers.

### Failure semantics

* **Acquire fails**: worker sleeps `check_interval` and retries. No
  work is performed.
* **Heartbeat fails mid-loop**: lease is considered lost, `on_lost`
  is invoked (best-effort), the supervised inner task is cancelled,
  and the next iteration attempts a fresh acquire. The previous lease
  owner has already been reaped by the backend (TTL expiry for Redis,
  session disconnect for Postgres).
* **Graceful shutdown**: `finally` block runs `release(name)` so the
  next worker picks up immediately instead of waiting for TTL.
* **Catastrophic shutdown (process kill)**: Postgres advisory lock is
  released when the connection is torn down by the server. Redis lease
  evicts after `ttl_seconds` (default 60 s).

### Observability

Every transition emits a structured log event:
`leader_acquired`, `leader_lease_lost`, `supervised_leader_started`,
`supervised_leader_lease_lost`, `supervised_task_crashed`,
`supervised_task_exited_restarting`, `leader_work_fn_failed`.
Aggregate `leader_acquired` by `name` + `identity` in your logs to see
which worker currently owns each loop.

---

## Status snapshot

| # | Area                                | Status   |
|---|-------------------------------------|----------|
| 1 | SQLite WAL + pragmas                | ✅        |
| 2 | Persist call state                  | ✅        |
| 3 | Hybrid mesh/SFU topology            | ✅        |
| 4 | Resumable chunked uploads           | ✅        |
| 5 | Alembic migrations (001..005)       | ✅        |
| 6 | Frontend topology + upload wiring   | ✅        |
| 7 | ResumableUploader.ts                | ✅        |
| 8 | GroupCallManager topology switch    | ✅        |
| 9 | Server/client event contract        | ✅        |
| 10| TopologyCoordinator in CallEngine   | ✅        |
| 11| Client call_heartbeat loop          | ✅        |
| 12| Server call_heartbeat + counters    | ✅        |
| 13| ResumableUploader token refresh     | ✅        |
| 14| FileDropManager → ResumableUploader | ✅        |
| 15| Alembic auto-upgrade on startup     | ✅        |
| 16| models/__init__ + renegotiate       | ✅        |
| 17| Replay truncation + full renegotiate| ✅        |
| 18| Upload + topology tests             | ✅        |
| 19| Init response hardening             | ✅        |
| 20| Auto-replay on reconnect            | ✅        |
| 21| Plug SFU router leak on end         | ✅        |
| 22| Restore topology on rehydrate       | ✅        |
| 23| DB orphan sweep                     | ✅        |
| 24| Build mediasoup SFU worker          | ✅        |
| 25| Server SFU socket handlers          | ✅        |
| 26| Client SFU adapter                  | ✅        |
| 27| Magic-byte MIME validation          | ✅        |
| 28| Upload rate-limit + quota           | ✅        |
| 29| Per-recipient file acceptance       | ✅        |
| 30| SFU BWE + producer pause            | ✅        |
| 31| SFU active speaker                  | ✅        |
| 32| SFU call recording                  | ✅        |
| 33| TURN relay fallback                 | ✅        |
| 34| @mentions dispatch                  | ✅        |
| 35| Messaging DLQ                       | ✅        |
| 36| Group-file multicast                | ✅        |
| 37| docs/ROADMAP_PROGRESS.md            | ✅ (this) |
| 38| Fix httpx AsyncClient(app=...) tests | ✅        |
| 39| Fix resumable-upload chunk_size tests| ✅        |
| 40| Wire desktop client to group-file multicast | ✅ |
| 41| Multi-worker leader election        | ✅        |
| 42| WebRTC data-channel P2P chunks      | ⏳ planned|

---

## Outstanding follow-ups (not yet ticketed)

* **Closed 2026-04-18**: Desktop-client wiring for group-file multicast.
  Shipped `CommClient-Desktop/src/renderer/services/filedrop/GroupFileMulticastManager.ts`
  (offer state machine, socket listeners, REST accept/reject/cancel,
  chunk fetch pool, peer discovery) and extended `FileDropManager` with
  `sendGroupFile(file, channelId, opts)` and `sendFileSmart(...)` (auto
  routes to multicast when recipient count crosses
  `groupMulticastThreshold`, default 3). Added `api.groupFileOffers.*`
  to the REST client. See §11 for the wire protocol.
* **Closed 2026-04-18**: Replaced pre-existing httpx `AsyncClient(app=...)`
  calls in `tests/conftest.py` + `tests/test_security_hardening.py` with
  the `ASGITransport(app=app)` form. This unblocks the legacy suites
  from the transport-layer construction error.
* **Closed 2026-04-18**: Resumable-upload tests (`tests/test_resumable_upload.py`)
  migrated to use `MIN_CHUNK_SIZE` (16 KiB) from the service so they
  stay aligned with the tightened production chunk-size range.
* **Closed 2026-04-18**: Multi-worker leader election for singleton
  background loops. Shipped `app/services/leader_election.py` with a
  pluggable `LeaderElectionBackend` ABC and three implementations:
  * `_SingleProcessBackend` — always leader, zero I/O (dev default).
  * `_PostgresAdvisoryBackend` — `pg_try_advisory_lock` with
    dedicated per-lock connections (released automatically when the
    session disconnects).
  * `_RedisBackend` — `SET NX PX` lease with Lua atomic
    refresh/release scripts keyed by a per-worker identity
    (`{host}:{pid}:{nonce}`).

  Public API: `try_acquire(name, ttl)`, `heartbeat(name, ttl)`,
  `release(name)`, `run_as_leader(LeaderLoopConfig(...))` for one-shot
  tick functions, and `run_supervised_as_leader(name, factory, ...)`
  for pre-existing services that own their own internal loops
  (`ScheduledMessageService.run_dispatch_loop`,
  `WebhookService.run_dispatch_loop`,
  `DeadLetterService._reaper_loop`).

  `app/main.py` now gates every singleton-sensitive loop:
  call-orphan sweeper, upload GC, WAL checkpoint, status-message
  expiry, channel-mute expiry, scheduled-message dispatcher, webhook
  dispatcher, poll expiry, DLQ reaper, group-file sweeper. The
  heartbeat-cleanup loop is intentionally NOT gated (it only
  disconnects sids local to each worker).

  Configured via `LEADER_ELECTION_BACKEND` (single|postgres|redis),
  `REDIS_URL`, and `LEADER_LEASE_TTL_SECONDS` in `app/core/config.py`.
  Auto-detection falls back to `postgres` when `DB_BACKEND=postgresql`,
  otherwise `single`.

* **New**: True WebRTC data-channel peer-to-peer transfer for group-file
  multicast. Today the `GroupFileMulticastManager` reports chunk
  ownership to the server but all bytes still come from the central
  file endpoint via range GETs. The next phase plumbs `get_chunk_peers`
  into a data-channel pull so bandwidth truly stays on the LAN swarm.
