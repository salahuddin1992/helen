# Admin API Integration Guide

This document describes the new admin API endpoints and services for server management, user administration, and database backups.

## Architecture Overview

### Services

#### 1. **MetricsService** (`app/services/metrics_service.py`)
Lightweight in-memory metrics collector for observability.

**Singleton:** `metrics_service`

**Methods:**
- `increment(metric_name, value=1)` — Atomically increment a counter
- `get_all() -> dict[str, int]` — Return snapshot of all counters
- `get_uptime() -> float` — Return server uptime in seconds
- `reset()` — Reset all counters (testing only)

**Default Counters:**
- `messages_sent_total` — Total messages transmitted
- `calls_initiated_total` — Total calls initiated
- `files_uploaded_total` — Total files uploaded
- `socket_connections_total` — Total socket connections (cumulative)
- `api_requests_total` — Total HTTP requests

**Usage Example:**
```python
from app.services.metrics_service import metrics_service

# Increment a counter
await metrics_service.increment("messages_sent_total", 1)

# Get all metrics
metrics = await metrics_service.get_all()
print(f"Messages: {metrics['messages_sent_total']}")
print(f"Uptime: {metrics_service.get_uptime()}s")
```

---

#### 2. **BackupService** (`app/services/backup_service.py`)
Database backup and restore with rotation policy.

**Singleton:** `backup_service`

**Backup Storage:** `data/backups/commclient_backup_YYYYMMDD_HHMMSS.db`

**Methods:**

```python
async def create_backup() -> str
```
Create a timestamped backup of SQLite database. Returns backup filename.

```python
async def list_backups() -> list[dict]
```
Return list of backups: `[{"name": str, "size_bytes": int, "created_at": ISO string}, ...]`

```python
async def restore_backup(backup_name: str) -> bool
```
Restore a backup (overwrites active DB). Creates protective backup before restore.
**DANGEROUS** — should be preceded by audit logging.

```python
async def delete_backup(backup_name: str) -> bool
```
Delete a specific backup file.

```python
async def auto_cleanup(keep_count: int = 10) -> int
```
Delete old backups, keeping only N most recent. Returns count deleted.

```python
async def get_db_size() -> int
```
Return size of current database in bytes.

**Usage Example:**
```python
from app.services.backup_service import backup_service

# Create backup
backup_name = await backup_service.create_backup()
print(f"Created: {backup_name}")

# List all
backups = await backup_service.list_backups()
for backup in backups:
    print(f"{backup['name']}: {backup['size_bytes']} bytes")

# Auto-cleanup (keep 10 most recent)
deleted = await backup_service.auto_cleanup(keep_count=10)
print(f"Deleted {deleted} old backups")
```

---

#### 3. **AdminService** (`app/services/admin_service.py`)
Server diagnostics, user management, and system-level operations.

**Singleton:** `admin_service`

**Methods:**

```python
async def get_server_stats() -> dict
```
Return comprehensive server statistics:
```json
{
  "uptime_seconds": 3600.5,
  "total_users": 42,
  "online_users": 15,
  "total_channels": 8,
  "total_messages": 1250,
  "total_files": 45,
  "total_calls": 120,
  "db_size_bytes": 52428800,
  "active_socket_connections": 22,
  "server_version": "1.0.0",
  "hostname": "desktop-ubuntu",
  "lan_ip": "192.168.1.100",
  "memory_usage_mb": 185.5,
  "cpu_percent": 8.3,
  "timestamp": "2026-04-08T20:30:00+00:00"
}
```

```python
async def get_active_calls() -> list[dict]
```
Return list of active calls with participant details.

```python
async def kick_user(user_id: str) -> bool
```
Force disconnect all socket connections for a user. Does not ban.

```python
async def ban_user(db: AsyncSession, user_id: str) -> bool
```
Soft-ban user (set `is_active=False`). Also kicks if connected.

```python
async def unban_user(db: AsyncSession, user_id: str) -> bool
```
Unban user (set `is_active=True`).

```python
async def cleanup_expired_sessions(db: AsyncSession) -> int
```
Delete expired JWT sessions from DB. Returns count deleted.

```python
async def cleanup_orphaned_files(db: AsyncSession) -> int
```
Delete file records with no message reference. Currently deferred for safety.

---

### REST API Endpoints

Base path: `/api/admin` (all require authentication)

#### Server Statistics

**GET `/api/admin/stats`**
```bash
curl -H "Authorization: Bearer <token>" http://localhost:3000/api/admin/stats
```

Response: Server statistics object (see AdminService.get_server_stats)

---

**GET `/api/admin/active-calls`**
```bash
curl -H "Authorization: Bearer <token>" http://localhost:3000/api/admin/active-calls
```

Response:
```json
{
  "calls": [
    {
      "call_id": "uuid",
      "initiator_id": "user-id",
      "call_type": "video",
      "routing": "sfu",
      "status": "active",
      "participant_count": 3,
      "participants": {
        "user-id-1": {"muted": false, "video_off": false},
        "user-id-2": {"muted": true, "video_off": false}
      },
      "created_at": "2026-04-08T20:25:00+00:00"
    }
  ],
  "count": 1
}
```

---

#### User Management

**POST `/api/admin/kick/{user_id}`**
Force disconnect a user (doesn't ban).

```bash
curl -X POST -H "Authorization: Bearer <token>" \
  http://localhost:3000/api/admin/kick/user-123
```

Response:
```json
{"status": "kicked", "user_id": "user-123"}
```

---

**POST `/api/admin/ban/{user_id}`**
Ban user (soft ban: `is_active=False`). Prevents login.

```bash
curl -X POST -H "Authorization: Bearer <token>" \
  http://localhost:3000/api/admin/ban/user-456
```

Response:
```json
{"status": "banned", "user_id": "user-456"}
```

---

**POST `/api/admin/unban/{user_id}`**
Unban user.

```bash
curl -X POST -H "Authorization: Bearer <token>" \
  http://localhost:3000/api/admin/unban/user-456
```

Response:
```json
{"status": "unbanned", "user_id": "user-456"}
```

---

#### Maintenance & Cleanup

**POST `/api/admin/cleanup/sessions`**
Delete expired JWT sessions.

```bash
curl -X POST -H "Authorization: Bearer <token>" \
  http://localhost:3000/api/admin/cleanup/sessions
```

Response:
```json
{"status": "cleanup_completed", "deleted_count": 5}
```

---

**POST `/api/admin/cleanup/files`**
Delete orphaned file records (currently deferred).

```bash
curl -X POST -H "Authorization: Bearer <token>" \
  http://localhost:3000/api/admin/cleanup/files
```

Response:
```json
{"status": "cleanup_completed", "deleted_count": 0}
```

---

#### Backup Management

**GET `/api/admin/backups`**
List all backups.

```bash
curl -H "Authorization: Bearer <token>" http://localhost:3000/api/admin/backups
```

Response:
```json
{
  "backups": [
    {
      "name": "commclient_backup_20260408_143000.db",
      "size_bytes": 52428800,
      "created_at": "2026-04-08T14:30:00+00:00"
    },
    {
      "name": "commclient_backup_20260408_120000.db",
      "size_bytes": 52000000,
      "created_at": "2026-04-08T12:00:00+00:00"
    }
  ],
  "count": 2
}
```

---

**POST `/api/admin/backups`**
Create a new backup.

```bash
curl -X POST -H "Authorization: Bearer <token>" \
  http://localhost:3000/api/admin/backups
```

Response:
```json
{
  "status": "backup_created",
  "backup_name": "commclient_backup_20260408_143045.db",
  "metadata": {
    "name": "commclient_backup_20260408_143045.db",
    "size_bytes": 52428800,
    "created_at": "2026-04-08T14:30:45+00:00"
  }
}
```

---

**POST `/api/admin/backups/{backup_name}/restore`**
Restore a specific backup. **DANGEROUS** — overwrites active database.

```bash
curl -X POST -H "Authorization: Bearer <token>" \
  http://localhost:3000/api/admin/backups/commclient_backup_20260408_120000.db/restore
```

Response:
```json
{"status": "backup_restored", "backup_name": "commclient_backup_20260408_120000.db"}
```

**Notes:**
- Creates a protective backup of the current DB before restore
- Audit logged for forensics
- Should be followed by verification that data is intact

---

**DELETE `/api/admin/backups/{backup_name}`**
Delete a backup.

```bash
curl -X DELETE -H "Authorization: Bearer <token>" \
  http://localhost:3000/api/admin/backups/commclient_backup_20260408_120000.db
```

Response:
```json
{"status": "backup_deleted", "backup_name": "commclient_backup_20260408_120000.db"}
```

---

## Audit Logging

All admin operations are automatically logged to the security audit trail via `app.core.audit.audit_log()`:

```
admin.stats_requested — Server stats retrieved
admin.active_calls_requested — Call list retrieved
admin.kick_user — User kicked (includes target_user_id)
admin.ban_user — User banned (includes target_user_id)
admin.unban_user — User unbanned (includes target_user_id)
admin.cleanup_sessions — Sessions cleanup (includes deleted_count)
admin.cleanup_files — Files cleanup (includes deleted_count)
admin.backups_listed — Backups listed (includes count)
admin.backup_created — Backup created (includes backup_name, size_bytes)
admin.backup_restored — Backup restored (includes backup_name)
admin.backup_deleted — Backup deleted (includes backup_name)
```

All audit entries include:
- `user_id` — Admin who performed the action
- `timestamp` — ISO 8601 with timezone
- `success` — bool
- `details` — additional context (varies by event)

---

## Implementation Notes

### Metrics Service
- **Thread-safe:** Uses `asyncio.Lock` for counter increments
- **No persistence:** Metrics reset on server restart (by design for simplicity)
- **Extensible:** New counters can be created on-the-fly via `increment()`

### Backup Service
- **SQLite-focused:** Tailored for SQLite backups via `shutil.copy2`
- **Path traversal protection:** Validates backup names to prevent `../` attacks
- **Protective backups:** Auto-creates backup before restore
- **Safe deletion:** Only deletes files matching pattern `commclient_backup_*.db`

### Admin Service
- **Graceful degradation:** `psutil` optional for CPU/memory metrics
- **Safe user operations:** `kick_user()` doesn't ban; separate `ban_user()` for enforcement
- **In-memory state:** Reads active calls from `call_service` (not DB)
- **Presence tracking:** Uses async-safe `presence_service.get_online_user_ids()`

---

## Future Extensions

1. **Role-based access control** — Restrict admin endpoints by user role
2. **Persistent audit log** — Store audit events in DB with retention policy
3. **Backup auto-scheduling** — Cron jobs for automatic backups
4. **File cleanup** — Implement safe orphaned file deletion with confirmation
5. **Performance profiling** — Detailed metrics on message throughput, call duration, etc.
6. **System health checks** — Disk space, memory pressure, connection limits

---

## Testing

### Manual Test Script

```python
import httpx
import asyncio

BASE_URL = "http://localhost:3000/api"
TOKEN = "your-jwt-token-here"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

async def test_admin():
    async with httpx.AsyncClient() as client:
        # Get stats
        r = await client.get(f"{BASE_URL}/admin/stats", headers=HEADERS)
        print("Stats:", r.json())
        
        # List backups
        r = await client.get(f"{BASE_URL}/admin/backups", headers=HEADERS)
        print("Backups:", r.json())
        
        # Create backup
        r = await client.post(f"{BASE_URL}/admin/backups", headers=HEADERS)
        print("Created backup:", r.json())
        
        # Get active calls
        r = await client.get(f"{BASE_URL}/admin/active-calls", headers=HEADERS)
        print("Active calls:", r.json())
        
        # Cleanup sessions
        r = await client.post(f"{BASE_URL}/admin/cleanup/sessions", headers=HEADERS)
        print("Cleanup result:", r.json())

asyncio.run(test_admin())
```

---
