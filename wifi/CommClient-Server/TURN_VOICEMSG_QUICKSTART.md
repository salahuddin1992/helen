# TURN & Voice Messages — Quick Start

## Files Created

### TURN Relay Service
```
app/services/turn_service.py        (21 KB) — RFC 5766 TURN relay with credential management
app/schemas/turn.py                 (3.6 KB) — Pydantic schemas for TURN API
app/api/routes/turn.py              (11 KB) — REST endpoints
```

### Voice Messages
```
app/models/voice_message.py         (2.4 KB) — ORM model
app/schemas/voice_message.py        (2.3 KB) — Pydantic schemas
app/services/voice_message_service.py (14 KB) — Upload, storage, streaming
app/api/routes/voice_messages.py    (11 KB) — REST endpoints
app/socket/voice_handlers.py        (11 KB) — Real-time socket events
```

### Documentation
```
TURN_VOICEMSG_INTEGRATION.md        (Complete integration guide)
TURN_VOICEMSG_QUICKSTART.md         (This file)
```

---

## Minimum Integration (5 Steps)

### 1. Update Model Imports
**File:** `app/models/__init__.py`

```python
from app.models.voice_message import VoiceMessage

__all__ = [
    # ... existing models ...
    "VoiceMessage",
]
```

### 2. Create Database Migration
```bash
cd /path/to/CommClient-Server
alembic revision --autogenerate -m "Add VoiceMessage model"
alembic upgrade head
```

### 3. Register Routes & Handlers
**File:** Main app setup (or `app/api/__init__.py`)

```python
from fastapi import FastAPI
from app.api.routes import turn, voice_messages

app = FastAPI()
app.include_router(turn.router)
app.include_router(voice_messages.router)

# Import socket handlers (auto-registers with sio)
import app.socket.voice_handlers
```

### 4. Start TURN Service
**File:** Application startup (e.g., `run.py` or FastAPI lifespan)

```python
from app.services.turn_service import turn_service
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    await turn_service.start()
    yield
    await turn_service.stop()

app = FastAPI(lifespan=lifespan)
```

### 5. Test
```bash
# Generate credentials
curl -X POST http://localhost:3000/turn/credentials \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -d '{"username":"user123","ttl_seconds":3600}'

# Get stats
curl http://localhost:3000/turn/stats \
  -H "Authorization: Bearer <JWT_TOKEN>"

# Upload voice message
curl -X POST http://localhost:3000/voice-messages \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -F "file=@recording.mp3" \
  -F "duration_ms=5000" \
  -F "channel_id=abc123..."
```

---

## Architecture Overview

### TURN Relay Service

```
┌─────────────────────────────────────────────────────────┐
│                   TURN Service                          │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  Allocations Manager                                    │
│  ├─ Credential validation (HMAC-SHA1)                   │
│  ├─ Port allocation (49152-65535)                       │
│  └─ Lifetime management (auto-cleanup every 30s)        │
│                                                         │
│  Permissions & Channels                                 │
│  ├─ Permission system per allocation                    │
│  ├─ Channel binding for optimized relay                 │
│  └─ Expiry with automatic cleanup                       │
│                                                         │
│  Statistics                                             │
│  ├─ Active allocations count                            │
│  ├─ Bytes/packets relayed                               │
│  └─ Per-allocation metrics                              │
│                                                         │
└─────────────────────────────────────────────────────────┘

API Endpoints:
  POST   /turn/credentials                    Generate ephemeral creds
  POST   /turn/allocations                    Create relay allocation
  GET    /turn/allocations/{id}               Get allocation details
  POST   /turn/allocations/{id}/refresh       Extend lifetime
  DELETE /turn/allocations/{id}               Release allocation
  POST   /turn/allocations/{id}/permissions   Add peer permission
  POST   /turn/allocations/{id}/channels      Bind channel number
  GET    /turn/stats                          Service statistics
```

### Voice Messages System

```
┌─────────────────────────────────────────────────────────┐
│              Voice Message Service                      │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  Upload & Storage                                       │
│  ├─ Audio validation (MP3, WAV, OGG, WebM, AAC, FLAC)   │
│  ├─ File storage in ./data/files/voice_messages/        │
│  └─ Size limits (100 MB max)                            │
│                                                         │
│  Waveform Generation                                    │
│  ├─ Peak amplitude sampling from audio                  │
│  ├─ Normalized data (0.0-1.0) for UI                    │
│  └─ Cached in database (JSON column)                    │
│                                                         │
│  Streaming                                              │
│  ├─ Range request support (HTTP 206)                    │
│  ├─ Efficient bandwidth usage                           │
│  └─ Content-Type preservation                           │
│                                                         │
│  Metadata & Transcription                               │
│  ├─ Duration, file size, MIME type                      │
│  ├─ Optional transcription text                         │
│  └─ Read/unread status                                  │
│                                                         │
└─────────────────────────────────────────────────────────┘

API Endpoints:
  POST   /voice-messages                      Upload audio
  GET    /voice-messages/{id}                 Get metadata
  GET    /voice-messages/{id}/audio           Stream audio (Range support)
  GET    /voice-messages/channel/{id}         List channel messages
  PATCH  /voice-messages/{id}                 Update metadata
  DELETE /voice-messages/{id}                 Delete message

Socket Events:
  voice_message_sent      → Channel: new message notification
  voice_message_playing   → Channel: user listening indicator
  voice_message_stopped   → Channel: user stopped listening
  voice_message_deleted   → Channel: message deleted notification
```

---

## Key Features

### TURN Service
✓ Short-term credentials (RFC 5766)
✓ Async UDP/TCP allocation
✓ Permission & channel binding
✓ Auto-expiry with cleanup loop
✓ Comprehensive stats
✓ LAN-optimized (localhost relay)
✓ Production-grade error handling

### Voice Messages
✓ Multi-format audio support
✓ Automatic waveform visualization data
✓ HTTP Range request streaming
✓ Transcription support (async-ready)
✓ Read status tracking
✓ Real-time socket notifications
✓ Automatic file cleanup
✓ Indexed database queries

---

## Data Models

### VoiceMessage (SQL Table)
```
id (UUID)           — Message identifier
channel_id (FK)     — Target channel
sender_id (FK)      — Sender user
duration_ms         — Audio length
file_path           — Absolute path to audio file
file_size           — Bytes
mime_type           — audio/mpeg, audio/wav, etc.
waveform_data       — JSON: [0.1, 0.3, 0.8, ...]
transcription       — Optional text
is_read             — Boolean flag
created_at          — Timestamp
updated_at          — Timestamp
```

### TURN Allocation (In-Memory)
```
allocation_id       — Unique identifier
username            — Authenticated user
relay_ip:port       — Relay address
client_ip:port      — Client address
transport           — "udp" or "tcp"
lifetime_seconds    — Remaining validity
permissions         — Dict[peer_addr] → Permission
channels            — Set[ChannelBinding]
bytes_relayed       — Counter
packets_relayed     — Counter
```

---

## Configuration

**TURN Port Range:**
- Default: 50000-65535
- Avoids mediasoup: 40000-49999
- Configurable via MEDIASOUP_MIN_PORT, MEDIASOUP_MAX_PORT

**Voice Message Storage:**
- Directory: `{UPLOAD_DIR}/voice_messages/`
- Max file size: 100 MB
- Max duration: 3600000 ms (1 hour)
- Formats: MP3, WAV, OGG, WebM, AAC, FLAC

**TURN Cleanup:**
- Interval: 30 seconds
- Removes expired allocations
- Releases ports back to pool

---

## Production Checklist

- [ ] Database migration created and applied
- [ ] Routes registered in main app setup
- [ ] Socket handlers imported
- [ ] TURN service started in lifespan/startup
- [ ] Storage directory permissions verified (writable)
- [ ] TURN stats endpoint monitored
- [ ] Tests written for credential flow
- [ ] Tests written for voice upload/streaming
- [ ] Client integration for WebRTC TURN
- [ ] Client UI for voice message recording/playback
- [ ] Audio format validation tested
- [ ] Range request streaming tested
- [ ] Socket events integrated in frontend
- [ ] Error handling verified
- [ ] Logging reviewed for production level

---

## Testing Examples

### TURN Credentials
```bash
TOKEN="your-jwt-token"

# 1. Generate credentials
curl -X POST http://localhost:3000/turn/credentials \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"username":"user123","ttl_seconds":3600}'

# Response:
# {
#   "username": "1712700000:user123",
#   "password": "abc123...",
#   "ttl": 3600,
#   "realm": "commclient.local"
# }

# 2. Create allocation
curl -X POST http://localhost:3000/turn/allocations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "1712700000:user123",
    "password": "abc123...",
    "client_ip": "192.168.1.5",
    "client_port": 54321,
    "transport": "udp",
    "lifetime": 600
  }'

# 3. Check stats
curl http://localhost:3000/turn/stats \
  -H "Authorization: Bearer $TOKEN" | jq
```

### Voice Message Upload
```bash
TOKEN="your-jwt-token"
CHANNEL_ID="channel-uuid"

# Record audio first (client-side), then upload
curl -X POST http://localhost:3000/voice-messages \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@recording.mp3" \
  -F "duration_ms=5000" \
  -F "channel_id=$CHANNEL_ID"

# Response:
# {
#   "id": "voice123...",
#   "channel_id": "...",
#   "duration_ms": 5000,
#   "waveform_data": [0.1, 0.3, 0.8, ...],
#   "mime_type": "audio/mpeg",
#   ...
# }
```

### Voice Message Streaming
```bash
VOICE_ID="voice123..."

# Full file
curl http://localhost:3000/voice-messages/$VOICE_ID/audio \
  -H "Authorization: Bearer $TOKEN" \
  -o audio.mp3

# Partial (Range request)
curl -H "Range: bytes=0-102400" \
  http://localhost:3000/voice-messages/$VOICE_ID/audio \
  -H "Authorization: Bearer $TOKEN" \
  -o audio-chunk.mp3

# Returns 206 Partial Content with Content-Range header
```

---

## Common Issues

| Issue | Solution |
|-------|----------|
| TURN credentials rejected | Check TTL hasn't expired; verify HMAC calculation |
| "No available ports" | Check cleanup is running; increase port range |
| Audio file 404 | Verify storage directory exists; check file permissions |
| Waveform empty | For production, implement librosa-based decoding |
| Range request fails | Check HTTP Range header format (bytes=start-end) |
| Socket events not received | Import voice_handlers module in startup |
| Migration fails | Ensure VoiceMessage model registered in __init__.py |

---

## Next Phase

After integration:
1. **Speech-to-Text**: Add async Celery task for audio transcription
2. **Audio Optimization**: Implement librosa for better waveform generation
3. **Metrics**: Track TURN usage, voice message sizes, durations
4. **CDN**: Consider R2/S3 for audio storage in cloud deployments
5. **Compression**: Add audio codec selection for bandwidth optimization
6. **Search**: Index transcriptions for voice message search

---

## Support

See `TURN_VOICEMSG_INTEGRATION.md` for:
- Detailed integration steps
- Full API reference
- Client usage examples
- Database schema
- Error handling
- Production considerations
- Troubleshooting guide
