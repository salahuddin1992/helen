# Admin API Quick Start

## Setup
All admin endpoints are authenticated. Ensure you have a valid JWT Bearer token.

```bash
TOKEN="your-jwt-token-here"
BASE_URL="http://localhost:3000/api/admin"
```

## Essential Endpoints

### Server Health
```bash
# Get server stats (uptime, users, resources, etc.)
curl -H "Authorization: Bearer $TOKEN" $BASE_URL/stats

# View active calls
curl -H "Authorization: Bearer $TOKEN" $BASE_URL/active-calls
```

### Backup Management
```bash
# Create backup
curl -X POST -H "Authorization: Bearer $TOKEN" $BASE_URL/backups

# List backups
curl -H "Authorization: Bearer $TOKEN" $BASE_URL/backups

# Restore backup (DANGEROUS)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  $BASE_URL/backups/commclient_backup_20260408_143000.db/restore

# Delete backup
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  $BASE_URL/backups/commclient_backup_20260408_143000.db
```

### User Management
```bash
# Ban a user
curl -X POST -H "Authorization: Bearer $TOKEN" \
  $BASE_URL/ban/user-id-here

# Unban a user
curl -X POST -H "Authorization: Bearer $TOKEN" \
  $BASE_URL/unban/user-id-here

# Kick a user (disconnect, not ban)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  $BASE_URL/kick/user-id-here
```

### Maintenance
```bash
# Clean up expired JWT sessions
curl -X POST -H "Authorization: Bearer $TOKEN" \
  $BASE_URL/cleanup/sessions

# Clean up orphaned files
curl -X POST -H "Authorization: Bearer $TOKEN" \
  $BASE_URL/cleanup/files
```

## Key Points

- **All endpoints require Bearer token authentication**
- **Backup restore is dangerous** — creates protective backup automatically
- **User ban is soft** — doesn't delete user data, just prevents login
- **User kick is temporary** — user can reconnect immediately
- **All operations are audit logged** — check `app/core/audit` logs

## Testing with httpx (Python)

```python
import httpx
import json

TOKEN = "your-token"
BASE = "http://localhost:3000/api/admin"
headers = {"Authorization": f"Bearer {TOKEN}"}

# Create a backup
r = httpx.post(f"{BASE}/backups", headers=headers)
print(json.dumps(r.json(), indent=2))

# Get stats
r = httpx.get(f"{BASE}/stats", headers=headers)
print(json.dumps(r.json(), indent=2))
```

## Common Patterns

### Before Restore
1. Check current backups: `GET /backups`
2. Create protective backup: `POST /backups`
3. Restore target backup: `POST /backups/{name}/restore`
4. Verify data integrity

### Cleanup Routine
1. Create backup: `POST /backups`
2. Clean sessions: `POST /cleanup/sessions`
3. Auto-cleanup old backups: Implement scheduled task
4. Monitor with `GET /stats`

## Troubleshooting

**"Backup not found"** → List backups with `GET /backups` to see exact names

**"Cannot kick yourself"** → Self-administration is blocked (use different admin account)

**"Cannot ban yourself"** → Self-administration is blocked

**psutil unavailable** → Memory/CPU metrics will be 0.0 (not an error)

## See Also

- Full documentation: `/ADMIN_API_INTEGRATION.md`
- Architecture details: `/ADMIN_API_SUMMARY.txt`
