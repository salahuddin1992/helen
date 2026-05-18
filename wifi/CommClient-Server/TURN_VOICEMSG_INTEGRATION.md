# TURN Relay & Voice Messages Integration Guide

This guide covers integrating the new **TURN relay service** and **Voice messages** subsystems into CommClient Server.

## Overview

### New Subsystems

#### 1. TURN Relay Service (`app/services/turn_service.py`)
Production-grade TURN (Traversal Using Relays around NAT) implementation for LAN WebRTC.

**Features:**
- Short-term credential mechanism (HMAC-SHA1)
- UDP/TCP relay allocation with configurable port range (49152-65535)
- Permission system per allocation
- Channel binding for optimized relay
- Automatic allocation expiry (600s default) with cleanup
- Comprehensive stats collection

**Files Created:**
- `app/services/turn_service.py` — TURN relay implementation
- `app/schemas/turn.py` — Pydantic schemas
- `app/api/routes/turn.py` — REST endpoints

#### 2. Voice Messages (`app/services/voice_message_service.py`)
Full-featured voice message system with recording, storage, streaming, and waveform visualization.

**Features:**
- Audio upload with format validation (MP3, WAV, OGG, WebM, AAC, FLAC)
- Automatic waveform data generation for UI visualization
- HTTP Range request support for efficient streaming
- Transcription storage (for async speech-to-text)
- Automatic cleanup on deletion

**Files Created:**
- `app/models/voice_message.py` — Database model
- `app/schemas/voice_message.py` — Request/response schemas
- `app/services/voice_message_service.py` — Storage & streaming service
- `app/api/routes/voice_messages.py` — REST endpoints
- `app/socket/voice_handlers.py` — Real-time socket events

---

## Integration Steps

### Step 1: Update Database Models Import

Edit `app/models/__init__.py` to include the new model:

```python
from app.models.voice_message import VoiceMessage

__all__ = [
    # ... existing models ...
    "VoiceMessage",
]
```

### Step 2: Create Database Migration

Generate an Alembic migration for the VoiceMessage table:

```bash
cd /path/to/CommClient-Server

# Generate migration
alembic revision --autogenerate -m "Add VoiceMessage model"

# Review and apply
alembic upgrade head
```

### Step 3: Register Routes

Edit `app/api/__init__.py` (or main.py FastAPI setup) to include the new routes:

```python
from fastapi import FastAPI
from app.api.routes import turn, voice_messages

app = FastAPI()

# Include TURN routes
app.include_router(turn.router)

# Include voice message routes
app.include_router(voice_messages.router)
```

### Step 4: Register Socket Handlers

Edit `app/socket/__init__.py` or wherever socket handlers are registered to import the voice handlers:

```python
# This ensures voice socket event handlers are registered
import app.socket.voice_handlers
```

### Step 5: Initialize TURN Service

Edit your application startup code (e.g., `run.py` or FastAPI `lifespan`):

```python
import asyncio
from app.services.turn_service import turn_service

async def startup():
    """Start background services."""
    await turn_service.start()

async def shutdown():
    """Shutdown background services."""
    await turn_service.stop()

# With FastAPI lifespan context manager:
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    # Startup
    await turn_service.start()
    yield
    # Shutdown
    await turn_service.stop()

app = FastAPI(lifespan=lifespan)
```

### Step 6: Configure Storage Directories

The voice message service creates directories automatically. Ensure your `app/core/config.py` has:

```python
class Settings(BaseSettings):
    # File uploads directory
    UPLOAD_DIR: str = "./data/files"
    
    # Voice messages are stored in: ./data/files/voice_messages/
    # (auto-created by VoiceMessageService)
```

---

## API Endpoints Reference

### TURN Relay Endpoints

**Generate Credentials**
```
POST /turn/credentials
Body: { "username": "user123", "ttl_seconds": 3600 }
Response: { "username": "...", "password": "...", "ttl": 3600, "realm": "..." }
```

**Create Allocation**
```
POST /turn/allocations
Body: {
  "username": "...",          # From credentials response
  "password": "...",          # From credentials response
  "client_ip": "192.168.1.5",
  "client_port": 54321,
  "transport": "udp",
  "lifetime": 600
}
Response: {
  "allocation_id": "...",
  "relay_ip": "127.0.0.1",
  "relay_port": 49152,
  ...
}
```

**Get Allocation Details**
```
GET /turn/allocations/{allocation_id}
```

**Refresh Allocation**
```
POST /turn/allocations/{allocation_id}/refresh?lifetime=600
```

**Delete Allocation**
```
DELETE /turn/allocations/{allocation_id}
```

**Add Permission**
```
POST /turn/allocations/{allocation_id}/permissions
Body: {
  "peer_ip": "192.168.1.10",
  "peer_port": 54321,
  "lifetime": 300
}
```

**Bind Channel**
```
POST /turn/allocations/{allocation_id}/channels
Body: {
  "channel_number": 16384,  # 0x4000
  "peer_ip": "192.168.1.10",
  "peer_port": 54321
}
```

**Get Statistics**
```
GET /turn/stats
Response: {
  "active_allocations": 5,
  "allocations": [...],
  "total_allocations_created": 42,
  "total_bytes_relayed": 1234567,
  "total_packets_relayed": 5432
}
```

---

### Voice Message Endpoints

**Upload Voice Message**
```
POST /voice-messages
Form Data:
  - file: <audio file>
  - duration_ms: 5000
  - channel_id: "abc123..."

Response: VoiceMessageResponse { id, channel_id, duration_ms, waveform_data, ... }
```

**Get Voice Message Metadata**
```
GET /voice-messages/{voice_message_id}
Response: VoiceMessageResponse
```

**Stream Audio (with Range support)**
```
GET /voice-messages/{voice_message_id}/audio
Headers: Range: bytes=0-1023

Response: 206 Partial Content or 200 OK with audio data
```

**List Channel Messages**
```
GET /voice-messages/channel/{channel_id}?limit=50&offset=0
Response: VoiceMessageListResponse {
  messages: [...],
  total: 150,
  has_more: true,
  limit: 50
}
```

**Update Message**
```
PATCH /voice-messages/{voice_message_id}
Body: {
  "is_read": true,
  "transcription": "Hello world"  # Optional
}
Response: VoiceMessageResponse
```

**Delete Message**
```
DELETE /voice-messages/{voice_message_id}
Status: 204 No Content
```

---

## Socket Events

### Voice Message Events

**When a voice message is sent:**
```javascript
// Server broadcasts to channel:
socket.on('voice_message_sent', (data) => {
  // {
  //   channel_id: "...",
  //   voice_message_id: "...",
  //   sender_id: "...",
  //   duration_ms: 5000,
  //   mime_type: "audio/mpeg",
  //   waveform_data: [0.1, 0.3, 0.8, ...],
  //   created_at: "2026-04-09T..."
  // }
});
```

**When user starts playing a message:**
```javascript
socket.emit('voice_message_playing', {
  channel_id: "...",
  voice_message_id: "..."
});

socket.on('voice_message_playing', (data) => {
  // { user_id: "...", channel_id: "...", voice_message_id: "..." }
});
```

**When user stops playing:**
```javascript
socket.emit('voice_message_stopped', {
  channel_id: "...",
  voice_message_id: "..."
});

socket.on('voice_message_stopped', (data) => {
  // { user_id: "...", channel_id: "..." }
});
```

**When a message is deleted:**
```javascript
socket.on('voice_message_deleted', (data) => {
  // { channel_id: "...", voice_message_id: "..." }
});
```

---

## Client Usage Examples

### TURN Credential Flow (WebRTC)

```typescript
// 1. Generate credentials
const credResponse = await fetch('/turn/credentials', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: 'user123', ttl_seconds: 3600 })
});
const creds = await credResponse.json();

// 2. Configure WebRTC with TURN servers
const peerConnection = new RTCPeerConnection({
  iceServers: [
    {
      urls: [`turn:127.0.0.1:${creds.relay_port}`],
      username: creds.username,
      credential: creds.password
    }
  ]
});
```

### Voice Message Upload

```typescript
// 1. Record audio (use recorder.js or Web Audio API)
const audioBlob = new Blob([audioData], { type: 'audio/mpeg' });

// 2. Upload to server
const formData = new FormData();
formData.append('file', audioBlob, 'recording.mp3');
formData.append('duration_ms', 5000);
formData.append('channel_id', channelId);

const response = await fetch('/voice-messages', {
  method: 'POST',
  body: formData
});
const voiceMessage = await response.json();

// 3. Emit socket event for real-time notification
socket.emit('voice_message_sent', {
  channel_id: voiceMessage.channel_id,
  voice_message_id: voiceMessage.id,
  duration_ms: voiceMessage.duration_ms,
  mime_type: voiceMessage.mime_type,
  waveform_data: voiceMessage.waveform_data
});
```

### Voice Message Streaming

```typescript
// 1. Get metadata
const response = await fetch(`/voice-messages/${voiceId}`);
const metadata = await response.json();

// 2. Stream audio with Range requests (for efficient seeking)
const audioResponse = await fetch(
  `/voice-messages/${voiceId}/audio`,
  { headers: { 'Range': 'bytes=0-102400' } }  // First 100KB
);
const audioChunk = await audioResponse.arrayBuffer();

// 3. Use waveform data for UI visualization
const canvas = document.getElementById('waveform');
drawWaveform(canvas, metadata.waveform_data);

// 4. Notify when playing starts
socket.emit('voice_message_playing', {
  channel_id: metadata.channel_id,
  voice_message_id: metadata.id
});

// Stop when done
socket.emit('voice_message_stopped', {
  channel_id: metadata.channel_id,
  voice_message_id: metadata.id
});
```

---

## Configuration

### TURN Service Settings

In `app/core/config.py`:

```python
class Settings(BaseSettings):
    # TURN relay port range (avoid mediasoup range)
    # Default: 50000-65535 (reserves mediasoup: 40000-49999)
    MEDIASOUP_MIN_PORT: int = 40000
    MEDIASOUP_MAX_PORT: int = 49999
    
    # TURN uses ports above MEDIASOUP_MAX_PORT + 1000
```

### Voice Message Storage

Voice messages stored in: `{UPLOAD_DIR}/voice_messages/`

Example structure:
```
./data/files/
├── voice_messages/
│   ├── abc123def456.mp3
│   ├── xyz789uvw012.wav
│   └── ...
└── (regular file uploads)
```

---

## Database Schema

### VoiceMessage Table

```sql
CREATE TABLE voice_messages (
    id VARCHAR(32) PRIMARY KEY,
    channel_id VARCHAR(32) NOT NULL,
    sender_id VARCHAR(32) NOT NULL,
    duration_ms INTEGER NOT NULL,
    file_path VARCHAR(512) NOT NULL,
    file_size INTEGER NOT NULL,
    mime_type VARCHAR(32) NOT NULL DEFAULT 'audio/mpeg',
    waveform_data TEXT,  -- JSON array
    transcription TEXT,
    is_read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
    FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_channel (channel_id),
    INDEX idx_sender (sender_id)
);
```

---

## Error Handling

### Common Error Responses

**TURN Errors:**
- `401 Unauthorized` — Invalid or expired credentials
- `404 Not Found` — Allocation not found
- `503 Service Unavailable` — No available relay ports

**Voice Message Errors:**
- `400 Bad Request` — Invalid audio format or duration
- `404 Not Found` — Voice message not found
- `413 Payload Too Large` — Audio file exceeds 100 MB
- `416 Range Not Satisfiable` — Invalid Range header

---

## Testing

### TURN Service Tests

```python
import pytest
from app.services.turn_service import turn_service

@pytest.mark.asyncio
async def test_generate_credentials():
    creds = turn_service.generate_credentials("user123", ttl_seconds=3600)
    assert "username" in creds
    assert "password" in creds
    assert creds["ttl"] == 3600

@pytest.mark.asyncio
async def test_allocation_lifecycle():
    # Generate credentials
    creds = turn_service.generate_credentials("user123")
    
    # Create allocation
    alloc = await turn_service.create_allocation(
        username=creds["username"],
        password=creds["password"],
        client_ip="192.168.1.5",
        client_port=54321,
        transport="udp"
    )
    assert alloc.relay_port > 0
    
    # Add permission
    await turn_service.add_permission(
        alloc.allocation_id,
        "192.168.1.10",
        54321
    )
    
    # Check permission
    has_perm = await turn_service.has_permission(
        alloc.allocation_id,
        "192.168.1.10",
        54321
    )
    assert has_perm
```

### Voice Message Tests

```python
@pytest.mark.asyncio
async def test_upload_and_retrieve(db, test_channel, test_user):
    # Create mock file
    audio_content = b"fake audio data..."
    file = UploadFile(
        filename="test.mp3",
        file=BytesIO(audio_content),
        size=len(audio_content),
        content_type="audio/mpeg"
    )
    
    # Upload
    vm = await VoiceMessageService.upload_voice_message(
        db=db,
        channel_id=test_channel.id,
        sender_id=test_user.id,
        file=file,
        duration_ms=5000
    )
    
    assert vm.id
    assert vm.duration_ms == 5000
    assert vm.waveform_data is not None
    
    # Retrieve
    retrieved = await VoiceMessageService.get_voice_message(db, vm.id)
    assert retrieved.id == vm.id
```

---

## Production Considerations

### Performance
- **TURN Cleanup**: Runs every 30 seconds, removes expired allocations
- **Waveform Caching**: Stored in JSON column, no recomputation on access
- **Audio Streaming**: Supports Range requests for efficient bandwidth use
- **Database Indexing**: Voice messages indexed by channel_id and sender_id

### Security
- TURN credentials expire after specified TTL
- Short-term authentication prevents credential replay
- Channel membership verified before socket events
- Audit logging on unauthorized access attempts
- Audio files isolated in separate directory

### Storage
- Voice messages stored with unique filenames (UUID-based)
- Auto-cleanup when messages deleted
- Supports files up to 100 MB
- Waveform data cached in database

### Monitoring
- Comprehensive logging with structlog
- TURN stats endpoint for monitoring allocations
- Voice message audit trail in logs
- Error isolation in socket event handlers

---

## Troubleshooting

### TURN Service Issues

**"No available relay ports"**
- Check port range configuration (MEDIASOUP_MIN_PORT, MEDIASOUP_MAX_PORT)
- Verify allocations are being cleaned up (check stats endpoint)
- Consider increasing MAX_PORT limit

**Invalid credentials error**
- Ensure TTL hasn't expired
- Verify HMAC calculation matches server
- Check credential format: "timestamp:username"

### Voice Message Issues

**"Audio file not found"**
- Check storage path exists
- Verify file permissions
- Ensure upload directory is writable

**Waveform data empty**
- Audio file may be corrupted
- Check MIME type validation
- For production, implement proper audio decoding (librosa)

---

## Next Steps

1. Run database migration for VoiceMessage table
2. Register routes and socket handlers
3. Initialize TURN service on startup
4. Test credential generation and allocation lifecycle
5. Test voice message upload and streaming
6. Integrate with Electron client UI
7. Add speech-to-text processing for transcriptions (async task)
8. Monitor TURN stats in production
