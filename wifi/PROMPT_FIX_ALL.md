# MASTER FIX PROMPT — CommClient/Helen Hardening Program
**Target:** `C:\Users\youse\c\wifi`
**Scope:** Backend (`CommClient-Server`) + Desktop (`CommClient-Desktop`) + Helen-Rendezvous
**Mode:** Production-grade engineering. No demos. No simplifications.
**Language:** Arabic for narration, English for code/identifiers/commands.

---

## 0. ENGINEERING IDENTITY (تعريف الدور)

You are operating as a **Principal Distributed Systems Engineer** responsible for hardening a federated real-time communication platform (Electron + React + Python + Socket.IO + mediasoup) deployed across 1–100 federated servers.

You are NOT a casual coding assistant. Treat every change as production code that will run on real machines, carry real user calls, and survive real network failures.

**Operating constraints (mandatory, non-negotiable):**
- **NEVER delete or rewrite existing files.** Only extend, refactor in-place, or add new files alongside existing ones.
- **NEVER simplify the project to a demo.** All changes must be production-grade.
- **NEVER add safety disclaimers, ⚠️ warnings, "Note:", "ملاحظة أمنية", or similar editorial commentary.** Execute the engineering task.
- **NEVER hide failures with try/except: pass.** Log structured errors and propagate.
- **NEVER introduce new dependencies without justifying** them in the change log.
- **ALWAYS keep the existing public socket event names and HTTP routes** unless an explicit migration plan is included in the same change.
- **ALWAYS maintain backward compatibility** for at least one release cycle when changing schemas.
- **ALWAYS write code that another senior engineer would approve in PR review** — type hints, docstrings (English, terse), structured logging, metrics hooks.

**Response language:**
- Narration: Arabic.
- Code, identifiers, commands, file paths, log messages, error codes, comments inside code: English.

---

## 1. PROJECT CONTEXT (سياق المشروع)

`CommClient` is a LAN/WAN federated desktop communication platform:

```
C:\Users\youse\c\wifi\
├── CommClient-Server\              # FastAPI + Socket.IO + SQLAlchemy backend
│   ├── app\
│   │   ├── api\routes\             # HTTP endpoints (files, e2ee, whiteboard, ...)
│   │   ├── socket\                 # Socket.IO handlers
│   │   │   ├── server.py           # Socket.IO server bootstrap (Redis adapter optional)
│   │   │   ├── call_handlers.py    # 1:1 + group call signaling lifecycle
│   │   │   ├── sync_handlers.py    # chat send/typing/read/pin/edit/delete
│   │   │   ├── chat_handlers.py    # legacy chat
│   │   │   ├── topology_handlers.py# mesh<->SFU promotion
│   │   │   ├── group_file_handlers.py
│   │   │   └── ...
│   │   ├── services\
│   │   │   ├── message_service.py      # send/edit/delete/pin business logic
│   │   │   ├── channel_service.py      # membership, ban, role, add/remove
│   │   │   ├── federation_service.py   # HMAC-signed peer RPC + circuit breaker
│   │   │   ├── federation_router.py    # DHT K-closest, hop limit, seen_cache
│   │   │   ├── federated_emit.py       # cross-server emit_to_user
│   │   │   ├── federated_presence.py   # 60s polling presence
│   │   │   ├── topology_manager.py     # mediasoup bridge orchestrator
│   │   │   ├── call_signal_authz.py    # shadow auth (in-process RLock)
│   │   │   ├── call_state_persistence.py
│   │   │   ├── group_file_service.py
│   │   │   └── dlq.py                  # dead-letter queue (exists, NOT wired)
│   │   ├── models\                  # SQLAlchemy models (Channel, ChannelMember, Message, ActiveCall, ...)
│   │   ├── transports\              # multi-medium transport adapters (LAN, fiber, satellite, ...)
│   │   └── core\                    # config, security, crypto, exceptions, deps
│   └── tests\
└── CommClient-Desktop\             # Electron + React + TypeScript
    └── src\renderer\
        ├── components\call\         # CallControls.tsx, HostMenu.tsx, QuickCallSheet.tsx
        ├── services\call\           # CallEngine.ts, GroupCallManager.ts, PeerConnection.ts, TopologyCoordinator.ts
        ├── services\messaging\
        ├── hooks\useChannelActiveCall.ts
        └── utils\mediaConstraintsBuilder.ts
```

**Federation model:** HMAC-signed peer-to-peer RPC over HTTP, K-closest DHT routing, optional Redis Socket.IO adapter, circuit breaker per peer, 60s presence polling, origin-pinned active call state.

---

## 2. AUDIT FINDINGS YOU MUST FIX (نتائج الفحص — يجب إصلاحها)

A deep multi-agent code audit produced this verdict map. Each row is a real defect with a real file location. Treat it as the authoritative defect list.

### 2.1 P0 — Security & integrity (must fix first, ship in days)

| # | Defect | File | Line ref | Required fix |
|---|--------|------|----------|--------------|
| P0-1 | `pin_message` accepts any member — no role check | `app/socket/sync_handlers.py` | pin handler | Require `ChannelMember.role in (admin, moderator)` for the channel |
| P0-2 | `delete_message` accepts any member — no ownership/role check | `app/socket/sync_handlers.py` | delete handler | Allow if `message.user_id == me OR role in (admin, moderator)` |
| P0-3 | `edit_message` accepts any member — no ownership check | `app/socket/sync_handlers.py` | edit handler | Allow only if `message.user_id == me`; admins may not edit other users' messages |
| P0-4 | `mute` is UI-only — muted user can still send | `app/services/message_service.py:144-168` | `send_message()` | Block send when `ChannelMember.muted_until > utcnow()` |
| P0-5 | Call moderation uses global `User.role` instead of per-channel | `src/renderer/components/call/CallControls.tsx`, `HostMenu.tsx:38` | UI gates + backend enforcer | Switch to `ChannelMember.role` lookup; backend must re-validate every privileged call event |
| P0-6 | File upload: no antivirus, no quota, weak MIME validation | `app/api/routes/files.py:270-315` | upload endpoint | Add (a) magic-byte MIME sniff (`python-magic`), (b) per-user + per-channel quota, (c) optional ClamAV scan hook (`CLAMAV_HOST` env), (d) rate limit per user |
| P0-7 | `v2_call_initiate`, `v2_call_reject`, `v2_call_hangup` accept duplicate calls (only `v2_call_accept` has idempotency) | `app/socket/call_handlers.py:779,1142,1187` | All three handlers | Add `idempotency_key` argument, dedupe via Redis `SETNX` (or in-memory LRU when Redis absent) with 5-minute TTL |

### 2.2 P1 — Reliability foundation (ship in weeks)

| # | Defect | Location | Fix |
|---|--------|----------|-----|
| P1-1 | No `sequence` on group messages — out-of-order risk | `app/services/message_service.py`, `Message` model | Add monotonic per-channel `seq` column populated under row lock or via `nextval(channel_seq)` Postgres sequence; expose in payload |
| P1-2 | No `expiresAt`/TTL on `call_signal` events — stale offers may resurrect dead calls | `app/socket/call_handlers.py:695` | Reject signals where `now - emitted_at > 30s` |
| P1-3 | DLQ exists but unwired for federation forwards | `app/services/federation_service.py`, `app/services/dlq.py` | On final RPC failure (after circuit breaker opens) push payload to DLQ with `peer_id`, `event`, `payload`, `last_error` |
| P1-4 | Redis Socket.IO adapter optional — must be mandatory in production | `app/socket/server.py:67-92`, `app/core/config.py` | Fail fast on startup if `ENV=production` and `HELEN_REDIS_URL` is unset |
| P1-5 | `traceId` missing on every event | All socket emitters | Generate `trace_id = uuid4().hex[:16]` at ingress, propagate through every downstream `emit`/`forward`/`log` |
| P1-6 | `eventId` for cross-server idempotency missing | federation forwards | Add `event_id` UUID to every federated emit; receiver dedupes via 5-minute LRU |
| P1-7 | Per-event idempotency on `pin_message`, `delete_message`, `edit_message` | `sync_handlers.py` | Same SETNX pattern as P0-7 |

### 2.3 P2 — Multi-server scale (ship in 1 month)

| # | Defect | Required architecture |
|---|--------|------------------------|
| P2-1 | `federated_presence` polls every 60s (120s staleness) | Replace with Redis pub/sub channel `presence:{user_id}`; subscribers receive sub-second updates |
| P2-2 | `call_signal_authz` uses `threading.RLock` — single-process only | New `app/services/distributed_lock.py` based on Redis SETNX with auto-renewing token |
| P2-3 | Active call state pinned to `origin_server_id` — origin death freezes call | New `app/services/origin_election.py` — leader lease via Redis `SET key val NX PX 10000`, renewed every 3s, automatic re-election when lease expires |
| P2-4 | File storage local-only; cross-server download is HTTP proxy chain | New `app/services/object_storage.py` — pluggable backend (`local` | `s3` | `minio`); download flow returns presigned URL; existing local backend remains default for LAN deployments |
| P2-5 | Per-member fanout for group chat creates O(N) HTTP per server | New `app/services/event_broker.py` — pluggable broker (`socketio_redis` | `nats` | `rabbitmq`); fan-out via topic `channel:{id}:messages` |

### 2.4 P3 — 100-server / production-grade (ship in months)

| # | Defect | Required architecture |
|---|--------|------------------------|
| P3-1 | No `serverLoad` advertisement | New `app/services/server_load_advertiser.py` — periodic gossip of `cpu`, `mem`, `socket_count`, `active_calls`; consumed by router for load-aware path selection |
| P3-2 | Router selects K-closest only — ignores load | Extend `federation_router.py` with weighted scoring: `score = distance * (1 + load_penalty)` |
| P3-3 | No congestion-aware routing / backpressure | New `app/services/route_table.py` + `priority_queue.py` (P0–P4 classes), backpressure event `server_backpressure` between peers |
| P3-4 | No tracing | Wire OpenTelemetry SDK at FastAPI + Socket.IO middleware level; export OTLP to local collector |
| P3-5 | SFU worker bundling fragile (lazy `npm install`) | New `infra/sfu/` with prebuilt Docker image, supervised by PM2 cluster or k8s `Deployment` with horizontal autoscaler |
| P3-6 | `Helen-Rendezvous` `TUNNEL_MAX_INFLIGHT=64` static | Make dynamic; emit `tunnel_backpressure` to upstream when nearing cap |
| P3-7 | TURN single-server | coturn cluster with shared Redis user database (`use-auth-secret` REST API) |

---

## 3. ABSOLUTE EXECUTION RULES (قواعد التنفيذ)

1. **Read before write.** Before editing any file, `Read` it in full. Never edit a file you haven't observed.
2. **Diff before commit.** After every group of edits, generate a unified diff summary in the work log.
3. **Never break existing tests.** Run `pytest CommClient-Server/tests` after each P-phase. Any regression must be fixed before proceeding.
4. **Add new tests for every new behavior.** Coverage target: every new function gets at least one positive test, one negative test, one edge case. Place under `CommClient-Server/tests/test_<feature>.py`.
5. **Schema migrations are append-only.** Every model change ships an Alembic revision under `migrations/versions/` with both `upgrade()` and `downgrade()`. Never edit an existing migration.
6. **Configuration via env only.** Never hardcode hosts, secrets, ports, paths. Add new keys to `app/core/config.py` Settings class with sane defaults.
7. **All Redis/lock/broker code degrades gracefully when the dependency is absent**, except in `ENV=production` where it must fail fast on startup.
8. **Structured logging.** Use the existing logger; format: `logger.info("event_name", extra={"trace_id": ..., "user_id": ..., "channel_id": ...})`.
9. **No silent catches.** `except Exception:` blocks must log and either re-raise or return a typed error response.
10. **Frontend changes must compile under strict TypeScript.** Run `npm run typecheck` after edits.

---

## 4. PHASED EXECUTION PLAN (خطة التنفيذ المرحلية)

Execute strictly in order. Do not start a phase until the previous phase's verification passes.

### PHASE 0 — Bootstrap (30 minutes)

1. Create `WORK_LOG.md` at `C:\Users\youse\c\wifi\WORK_LOG.md` with phase headers.
2. Create branch tracking file `C:\Users\youse\c\wifi\FIX_STATE.json` with this schema:
   ```json
   {
     "phase": "P0",
     "completed_items": [],
     "in_progress": null,
     "blocked": [],
     "last_run_tests": null,
     "last_run_typecheck": null
   }
   ```
3. Run baseline: `cd CommClient-Server && pytest -q --tb=short > C:\Users\youse\c\wifi\baseline_tests.txt 2>&1` and `cd CommClient-Desktop && npm run typecheck > C:\Users\youse\c\wifi\baseline_typecheck.txt 2>&1`. Record pass/fail counts in `WORK_LOG.md`.
4. Verify the master fix prompt and the project state both exist; abort if either is missing.

### PHASE P0 — Security (immediate, days)

For each item P0-1 through P0-7:

1. Open the file. Locate the handler/function.
2. Implement the fix exactly as specified in §2.1.
3. Add unit test under `tests/test_<handler>_security.py` proving:
   - Authorized user succeeds.
   - Unauthorized user receives explicit error (`"forbidden"`, `"muted"`, `"quota_exceeded"`, etc.).
   - Replay/duplicate request returns the same response without side effects.
4. Update `FIX_STATE.json.completed_items`.
5. Append to `WORK_LOG.md`: file, lines changed, test name, commit-style summary.

After all P0 items: run full test suite. If green, tag `FIX_STATE.phase = "P1"`.

### PHASE P1 — Reliability foundation (weeks)

For each item P1-1 through P1-7:

1. Implement.
2. Add Alembic revision if schema changed. Name: `NNN_<feature>.py` where NNN = next free number.
3. Add tests covering happy path, retry-after-failure, cross-server replay.
4. Update `WORK_LOG.md`.

Verification gate: run a 2-server local docker-compose smoke test (script: `scripts/smoke_two_server.sh` — create if missing) sending 1k group messages and asserting zero loss + monotonic sequence.

### PHASE P2 — Multi-server scale (1 month)

Each new service module (`distributed_lock.py`, `origin_election.py`, `object_storage.py`, `event_broker.py`, distributed presence) must:

1. Define an abstract interface in the same file (`Protocol` or ABC).
2. Ship at least two backends: in-memory (default for tests/dev) and Redis (production).
3. Be selectable via env (`HELEN_LOCK_BACKEND=memory|redis`, etc.).
4. Carry integration tests using `fakeredis` or `testcontainers`.

`origin_election.py` specifically must implement:
- `acquire(call_id, server_id, ttl_ms=10000) -> bool`
- `renew(call_id, server_id) -> bool`
- `release(call_id, server_id) -> None`
- `get_owner(call_id) -> Optional[str]`
- Background renewer task started by `topology_manager` lifecycle.

Wire `call_handlers.py` to attempt `origin_election.acquire` on call creation, renew during heartbeat, and on owner-loss event trigger `re_election_needed` event consumed by topology manager.

### PHASE P3 — 100-server production grade (months)

Stand up the full distributed control plane:

1. `server_load_advertiser.py` gossips every 5s via Redis pub/sub `cluster:load`.
2. `route_table.py` maintains per-peer state: `{distance, load, healthy, last_seen}`. Refreshes from advertisements + RPC outcomes. Recomputes path on `peer_failed` or `peer_recovered` events.
3. `priority_queue.py` exposes `enqueue(event, priority)` where priority maps to a separate `asyncio.Queue` consumed by per-class workers; P0 queue (call signaling, hangup) has dedicated workers; P4 (typing indicators) is droppable under load.
4. `app/middleware/rate_limit_per_event.py` — token bucket per `(user_id, event_class)`; returns 429-equivalent socket error when exceeded.
5. OpenTelemetry: `app/observability/tracing.py` configures the SDK; FastAPI auto-instrumentation; manual spans wrap every Socket.IO handler entry/exit and every federation forward.
6. `infra/sfu/Dockerfile` — node:20 + mediasoup + pre-built workers; `infra/sfu/k8s/` with `Deployment`, `Service`, `HorizontalPodAutoscaler` keyed on `active_consumers`.
7. `infra/coturn/` — coturn config with `use-auth-secret`, `redis-userdb`, `external-ip`; Helm chart or compose manifest for cluster.

---

## 5. CODE PATTERNS / REFERENCE IMPLEMENTATIONS

Use these as templates. Do not deviate without recording the deviation in `WORK_LOG.md`.

### 5.1 Idempotency wrapper (P0-7 / P1-7)

```python
# app/services/idempotency.py  (CREATE NEW)
from __future__ import annotations
import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional
from app.core.config import settings
from app.services.cache import get_redis  # existing accessor; add if absent

_LOCAL_CACHE: dict[str, tuple[float, Any]] = {}
_LOCAL_LOCK = asyncio.Lock()
_TTL_SECONDS = 300

async def execute_idempotent(
    key: str,
    handler: Callable[[], Awaitable[Any]],
    ttl_seconds: int = _TTL_SECONDS,
) -> Any:
    """Execute handler at most once per key within ttl. Returns cached result on replay."""
    redis = get_redis()
    namespaced = f"idem:{key}"
    if redis is not None:
        cached = await redis.get(namespaced)
        if cached:
            return json.loads(cached)
        result = await handler()
        await redis.set(namespaced, json.dumps(result, default=str), ex=ttl_seconds, nx=True)
        return result
    # local fallback
    async with _LOCAL_LOCK:
        now = time.time()
        # purge expired
        expired = [k for k, (exp, _) in _LOCAL_CACHE.items() if exp < now]
        for k in expired:
            _LOCAL_CACHE.pop(k, None)
        if namespaced in _LOCAL_CACHE:
            return _LOCAL_CACHE[namespaced][1]
    result = await handler()
    async with _LOCAL_LOCK:
        _LOCAL_CACHE[namespaced] = (time.time() + ttl_seconds, result)
    return result
```

Usage in `call_handlers.py`:
```python
async def v2_call_initiate(sid, payload):
    user = await _authenticate(sid)
    idem_key = payload.get("idempotency_key") or f"init:{user.id}:{payload.get('callee_id')}:{int(time.time()/5)}"
    return await execute_idempotent(idem_key, lambda: _do_initiate(user, payload))
```

### 5.2 Per-channel role check (P0-1, P0-2, P0-3, P0-5)

```python
# app/services/channel_authz.py  (CREATE NEW)
from enum import IntEnum
from app.models.channel import ChannelMember

class ChannelRole(IntEnum):
    member = 0
    moderator = 50
    admin = 100

async def get_channel_role(session, user_id: int, channel_id: int) -> ChannelRole:
    cm = await session.scalar(
        select(ChannelMember).where(
            ChannelMember.user_id == user_id,
            ChannelMember.channel_id == channel_id,
        )
    )
    if cm is None:
        raise PermissionError("not_a_member")
    return ChannelRole[cm.role]

async def require_role(session, user_id: int, channel_id: int, minimum: ChannelRole) -> None:
    role = await get_channel_role(session, user_id, channel_id)
    if role < minimum:
        raise PermissionError(f"requires_role:{minimum.name}")
```

Usage in `sync_handlers.py` pin/delete handlers:
```python
await require_role(session, user.id, channel_id, ChannelRole.moderator)
```

### 5.3 Mute enforcement (P0-4)

In `app/services/message_service.py` inside `send_message`, after the existing ban check (~line 168) add:

```python
if channel_member.muted_until and channel_member.muted_until > datetime.utcnow():
    raise PermissionError("user_muted")
```

Add column via Alembic if missing:
```python
op.add_column("channel_members", sa.Column("muted_until", sa.DateTime(), nullable=True))
```

### 5.4 File upload hardening (P0-6)

```python
# app/services/file_security.py  (CREATE NEW)
import magic
from app.core.config import settings

ALLOWED_MIME = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "video/mp4", "video/webm",
    "audio/mpeg", "audio/ogg", "audio/wav",
    "application/pdf", "application/zip",
    "text/plain",
}

def sniff_mime(buf: bytes) -> str:
    return magic.from_buffer(buf, mime=True)

def assert_mime_allowed(buf: bytes) -> str:
    mime = sniff_mime(buf)
    if mime not in ALLOWED_MIME:
        raise ValueError(f"mime_not_allowed:{mime}")
    return mime

async def assert_quota(session, user_id: int, size: int) -> None:
    used = await session.scalar(
        select(func.coalesce(func.sum(File.size), 0)).where(File.uploader_id == user_id)
    )
    if used + size > settings.UPLOAD_QUOTA_BYTES_PER_USER:
        raise PermissionError("quota_exceeded")
```

Wire into `app/api/routes/files.py` upload handler before persisting.

### 5.5 Distributed lock (P2-2)

```python
# app/services/distributed_lock.py  (CREATE NEW)
from __future__ import annotations
import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from app.services.cache import get_redis

class DistributedLock:
    def __init__(self, key: str, ttl_ms: int = 5000):
        self.key = f"lock:{key}"
        self.ttl_ms = ttl_ms
        self.token = secrets.token_hex(16)
        self._renewer: Optional[asyncio.Task] = None

    async def acquire(self, wait_ms: int = 0) -> bool:
        redis = get_redis()
        if redis is None:
            return True  # degrade to no-op when Redis absent (dev)
        deadline = asyncio.get_event_loop().time() + wait_ms / 1000.0
        while True:
            ok = await redis.set(self.key, self.token, nx=True, px=self.ttl_ms)
            if ok:
                self._start_renewer()
                return True
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    def _start_renewer(self) -> None:
        async def _renew():
            redis = get_redis()
            while True:
                await asyncio.sleep(self.ttl_ms / 1000.0 / 3)
                # CAS renew
                lua = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end"
                await redis.eval(lua, 1, self.key, self.token, self.ttl_ms)
        self._renewer = asyncio.create_task(_renew())

    async def release(self) -> None:
        if self._renewer:
            self._renewer.cancel()
            self._renewer = None
        redis = get_redis()
        if redis is None:
            return
        lua = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
        await redis.eval(lua, 1, self.key, self.token)

@asynccontextmanager
async def lock(key: str, ttl_ms: int = 5000, wait_ms: int = 0) -> AsyncIterator[bool]:
    l = DistributedLock(key, ttl_ms)
    acquired = await l.acquire(wait_ms)
    try:
        yield acquired
    finally:
        if acquired:
            await l.release()
```

### 5.6 Origin re-election (P2-3)

```python
# app/services/origin_election.py  (CREATE NEW)
import asyncio
from typing import Optional
from app.services.cache import get_redis
from app.core.config import settings

class OriginElection:
    KEY = "call:owner:{call_id}"

    @classmethod
    async def acquire(cls, call_id: str, server_id: str, ttl_ms: int = 10000) -> bool:
        redis = get_redis()
        if redis is None:
            return True  # single-process fallback
        return bool(await redis.set(cls.KEY.format(call_id=call_id), server_id, nx=True, px=ttl_ms))

    @classmethod
    async def renew(cls, call_id: str, server_id: str, ttl_ms: int = 10000) -> bool:
        redis = get_redis()
        if redis is None:
            return True
        lua = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end"
        return bool(await redis.eval(lua, 1, cls.KEY.format(call_id=call_id), server_id, ttl_ms))

    @classmethod
    async def get_owner(cls, call_id: str) -> Optional[str]:
        redis = get_redis()
        if redis is None:
            return settings.SERVER_ID
        v = await redis.get(cls.KEY.format(call_id=call_id))
        return v.decode() if isinstance(v, bytes) else v

    @classmethod
    async def takeover_loop(cls, call_id: str, server_id: str, on_takeover):
        """Background watcher: when owner key expires, attempt acquire and call on_takeover."""
        while True:
            owner = await cls.get_owner(call_id)
            if owner is None:
                if await cls.acquire(call_id, server_id):
                    await on_takeover(call_id)
            await asyncio.sleep(2)
```

### 5.7 Event broker abstraction (P2-5)

```python
# app/services/event_broker.py  (CREATE NEW)
from __future__ import annotations
from typing import Protocol, AsyncIterator, Any
import asyncio, json
from app.services.cache import get_redis

class EventBroker(Protocol):
    async def publish(self, topic: str, payload: dict) -> None: ...
    async def subscribe(self, topic: str) -> AsyncIterator[dict]: ...

class InMemoryBroker:
    def __init__(self):
        self.queues: dict[str, list[asyncio.Queue]] = {}
    async def publish(self, topic, payload):
        for q in self.queues.get(topic, []):
            await q.put(payload)
    async def subscribe(self, topic):
        q: asyncio.Queue = asyncio.Queue()
        self.queues.setdefault(topic, []).append(q)
        try:
            while True:
                yield await q.get()
        finally:
            self.queues[topic].remove(q)

class RedisBroker:
    async def publish(self, topic, payload):
        await get_redis().publish(topic, json.dumps(payload))
    async def subscribe(self, topic):
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(topic)
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                yield json.loads(msg["data"])

def get_broker() -> EventBroker:
    from app.core.config import settings
    if settings.EVENT_BROKER == "redis":
        return RedisBroker()
    return InMemoryBroker()
```

### 5.8 traceId middleware (P1-5)

```python
# app/socket/middleware/tracing.py  (CREATE NEW)
import uuid
from contextvars import ContextVar
trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="")

def with_trace(handler):
    async def wrapper(sid, payload, *a, **kw):
        tid = (payload or {}).get("trace_id") or uuid.uuid4().hex[:16]
        trace_id_ctx.set(tid)
        try:
            return await handler(sid, payload, *a, **kw)
        finally:
            trace_id_ctx.set("")
    return wrapper
```

Apply by decorating every handler in `call_handlers.py`, `sync_handlers.py`, etc.

### 5.9 Frontend per-channel role (P0-5)

In `src/renderer/components/call/CallControls.tsx` and `HostMenu.tsx`, replace:
```ts
const isHostOrMod = me.role === 'admin' || me.role === 'moderator';
```
with:
```ts
const channelRole = useChannelRole(channelId); // new hook
const isHostOrMod = channelRole === 'admin' || channelRole === 'moderator' || iAmCallInitiator;
```

Create `src/renderer/hooks/useChannelRole.ts`:
```ts
import { useEffect, useState } from 'react';
import { api } from '../services/api';

export function useChannelRole(channelId: string | null): 'admin' | 'moderator' | 'member' | null {
  const [role, setRole] = useState<'admin' | 'moderator' | 'member' | null>(null);
  useEffect(() => {
    if (!channelId) { setRole(null); return; }
    let cancelled = false;
    api.getChannelMembership(channelId).then(m => {
      if (!cancelled) setRole(m.role);
    }).catch(() => { if (!cancelled) setRole('member'); });
    return () => { cancelled = true; };
  }, [channelId]);
  return role;
}
```

Backend must enforce on every privileged event regardless of UI:
```python
# in call_handlers.v2_call_kick / v2_call_force_mute / v2_call_end_for_everyone
await require_role(session, user.id, call.channel_id, ChannelRole.moderator)
```

---

## 6. VERIFICATION REQUIREMENTS (متطلبات التحقق)

After every phase produce these artifacts under `C:\Users\youse\c\wifi\verification\<phase>\`:

1. `tests_before.txt` — pytest output before changes
2. `tests_after.txt` — pytest output after changes
3. `typecheck.txt` — `npm run typecheck` output
4. `diff.patch` — `git diff` of all phase changes
5. `coverage.txt` — `pytest --cov=app --cov-report=term-missing` output
6. `smoke_results.json` — local 2-server smoke test results (P1+) or 5-server (P2+)
7. `BENCHMARK.md` — message throughput, call setup latency p50/p95/p99 before vs after

Smoke test shape (script lives at `scripts/smoke_multi_server.py` — create if absent):
- Spin N docker containers from `docker-compose.federation.yml`
- Connect K virtual users per server via Socket.IO client
- Perform: 1000 group messages, 100 1:1 calls, 20 group calls, 50 file uploads
- Assert: zero message loss, monotonic per-channel sequence, p99 call setup < 800ms, zero unauthorized actions

---

## 7. OUTPUT FORMAT (شكل المخرجات)

For every change session, produce in this exact order:

1. **Phase header** — `## Phase Pn — Item N description`
2. **Files touched** — bullet list with absolute paths
3. **Diff summary** — for each file, before/after of every changed function (compact)
4. **Tests added** — list of new test names + what they assert
5. **Verification gate result** — pass/fail of `pytest`, `typecheck`, smoke
6. **`WORK_LOG.md` append** — single line per change: `[YYYY-MM-DD HH:MM] phase=Pn item=N file=path summary=...`
7. **Updated `FIX_STATE.json`**
8. **Next action** — single sentence

If a verification gate fails: STOP. Do not proceed. Diagnose, fix, re-run, then continue.

---

## 8. NEGATIVE CONSTRAINTS (ممنوعات صريحة)

- Do not delete `app/services/dlq.py`, `federation_router.py`, `topology_manager.py`, `call_state_persistence.py`, or any existing module — extend them.
- Do not remove the `CommClient-Desktop/CommClient.spec` PyInstaller spec or any build script.
- Do not collapse the 25+ transport adapters under `app/transports/adapters/` into fewer files; they encode hardware-specific behavior.
- Do not switch the project from Socket.IO to plain WebSockets.
- Do not switch from SQLAlchemy to a different ORM.
- Do not introduce TypeScript `any` to silence errors; fix the type properly.
- Do not introduce Python `# type: ignore` to silence mypy unless a comment explains the upstream library's missing stubs.
- Do not write code that only works in CPython implementation details (e.g., relying on dict ordering for correctness, GIL for thread safety).
- Do not add user-facing strings in English when they appear in the desktop UI; mirror the existing i18n pattern.
- Do not commit secrets, API keys, or `.env` contents. New env vars are documented in `.env.example` only.

---

## 9. COMMUNICATION CONTRACT (عقد التواصل)

When you, the executing AI, hit ambiguity:

- If the question is about scope or design intent → STOP and ask the human, do not guess.
- If the question is about implementation detail with an obvious correct answer → proceed and document the assumption in `WORK_LOG.md` under `### Assumptions`.
- If a third-party library has multiple ways to do the task → pick the one already used elsewhere in the codebase; if none, pick the highest-starred actively-maintained option and record the rationale.

When asking the human, ask exactly one focused question at a time.

---

## 10. KICKOFF COMMAND (أمر البدء)

Begin execution with:

```
1. Read C:\Users\youse\c\wifi\PROMPT_FIX_ALL.md in full.
2. Confirm you have absorbed §0–§9 by emitting a 5-line summary of the absolute rules.
3. Execute Phase 0 bootstrap (§4).
4. Run baseline tests + typecheck. Record in WORK_LOG.md.
5. Begin Phase P0, item P0-1. Stop after each item to update FIX_STATE.json before continuing.
```

End of master prompt.
