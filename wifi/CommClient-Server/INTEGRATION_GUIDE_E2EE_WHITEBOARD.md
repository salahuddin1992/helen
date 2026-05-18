# CommClient E2EE & Whiteboard Integration Guide

This document covers integration of two major features:
1. **End-to-End Encryption (E2EE) Module** — X3DH key exchange + Double Ratchet infrastructure
2. **Collaborative Whiteboard** — Real-time drawing canvas with Socket.IO synchronization

---

## Project Structure

```
app/
├── models/
│   ├── e2ee_key.py              # E2EE models (IdentityKey, SignedPreKey, OneTimePreKey, E2EESession)
│   ├── whiteboard.py            # Whiteboard models (Session, Stroke, Snapshot)
│
├── schemas/
│   ├── e2ee.py                  # E2EE schemas (KeyBundleUpload, KeyBundleResponse, SessionEstablished, etc.)
│   ├── whiteboard.py            # Whiteboard schemas (Session, Stroke, Participant, etc.)
│
├── services/
│   ├── e2ee_service.py          # E2EE service (key management, session registration)
│   ├── whiteboard_service.py    # Whiteboard service (stroke management, participant tracking)
│
├── api/routes/
│   ├── e2ee.py                  # E2EE REST endpoints (POST /e2ee/keys, GET /e2ee/keys/{id}, etc.)
│   ├── whiteboard.py            # Whiteboard REST endpoints (POST /whiteboards, GET /whiteboards/{id}, etc.)
│
├── socket/
│   ├── e2ee_handlers.py         # E2EE socket events (key_bundle_updated, session_request, etc.)
│   ├── whiteboard_handlers.py   # Whiteboard socket events (join, stroke, undo, cursor_move, etc.)
```

**Lines of Code:** ~3,040 (production-grade, fully documented)

---

## E2EE Module Integration

### Overview

The E2EE module implements the **Signal Protocol** (X3DH key exchange + Double Ratchet):
- **Server role:** Key distribution server (stores public keys, pre-keys, signed pre-keys)
- **Plaintext security:** Server never sees plaintext messages, only key material
- **Key bundle components:**
  - Identity Key (long-term, immutable)
  - Signed Pre-Key (medium-term, rotatable)
  - One-Time Pre-Keys (single-use, batch-uploaded, consumed atomically)

### Database Schema

Create migration or add to alembic:

```python
# app/models/e2ee_key.py

class IdentityKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """User's permanent identity public key."""
    user_id: str (unique)
    public_key: bytes (base64)
    key_version: int

class SignedPreKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Medium-term pre-key, signed by identity key. Rotatable."""
    user_id: str
    key_id: int
    public_key: bytes (base64)
    signature: bytes (base64, ik signature of spk)
    is_active: bool (default True)
    Unique(user_id, key_id)

class OneTimePreKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Single-use pre-key. Consumed atomically on X3DH."""
    user_id: str
    key_id: int
    public_key: bytes (base64)
    used: bool (default False, indexed)
    used_by_user_id: str (nullable)
    used_at: datetime (nullable)
    Unique(user_id, key_id)

class E2EESession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Metadata for established encrypted session."""
    session_id: str (unique)
    initiator_id: str
    responder_id: str
    established_at: datetime
    last_message_at: datetime (nullable)
    is_active: bool (indexed)
```

### REST Endpoints

#### 1. Upload Key Bundle
```http
POST /e2ee/keys
Authorization: Bearer <token>

{
  "identity_key": "base64_encoded_public_key",
  "signed_pre_key": "base64_encoded_spk",
  "signed_pre_key_signature": "base64_signature_by_ik",
  "one_time_pre_keys": [
    "base64_otpk1",
    "base64_otpk2",
    ...
  ]
}

Response 200:
{
  "identity_key_version": 1,
  "signed_pre_key_id": 5,
  "one_time_pre_keys_stored": 100
}
```

**Logic:**
- Validates all keys as valid base64
- First upload: creates identity key (immutable thereafter)
- Rotates signed pre-key, deactivates old ones (retains last 2 for retransmission)
- Batch-inserts one-time pre-keys with sequential IDs
- Atomically persists all changes

#### 2. Fetch Key Bundle (for X3DH)
```http
GET /e2ee/keys/{target_user_id}
Authorization: Bearer <token>

Response 200:
{
  "identity_key": "base64...",
  "signed_pre_key": "base64...",
  "signed_pre_key_id": 5,
  "signed_pre_key_signature": "base64...",
  "one_time_pre_key": "base64..." OR null,
  "one_time_pre_key_id": 42 OR null
}
```

**Security:**
- Atomically consumes one OTP key (marks as used, records consumer)
- OTP consumption is race-condition safe (database-level transaction)
- Returns null for one_time_pre_key if supply exhausted
- Server-side emissions trigger when count < 10 (client should re-upload)

#### 3. Get Pre-Key Count
```http
GET /e2ee/keys/me/count
Authorization: Bearer <token>

Response 200:
{
  "remaining_pre_keys": 8,
  "should_rotate": true
}
```

#### 4. Rotate Signed Pre-Key
```http
POST /e2ee/keys/rotate
Authorization: Bearer <token>

{
  "signed_pre_key": "base64_new_spk",
  "signed_pre_key_signature": "base64_signature"
}

Response 200:
{
  "key_id": 6,
  "activated_at": "2026-04-09T10:30:00Z"
}
```

#### 5. Register E2EE Session
```http
POST /e2ee/sessions
Authorization: Bearer <token>

{
  "session_id": "hash_of_initial_dh_outputs",
  "initiator_id": "user_a_id",
  "responder_id": "user_b_id"
}

Response 200:
{
  "session_id": "...",
  "initiator_id": "...",
  "responder_id": "..."
}
```

**Security:** Caller must be initiator or responder.

### Socket.IO Events

#### e2ee:key_bundle_updated
```javascript
// User broadcasts they've rotated their key
{
  "new_spk_id": 6
}

// Server broadcasts to all peers
{
  "user_id": "alice_id",
  "new_spk_id": 6
}
```

#### e2ee:session_request
```javascript
// Initiator sends X3DH initial message to responder
{
  "responder_id": "bob_id",
  "key_agreement_data": "base64_x3dh_msg1"
}

// Server forwards to responder (or stores if offline)
```

#### e2ee:session_ack
```javascript
// Responder acknowledges X3DH completion
{
  "initiator_id": "alice_id",
  "session_id": "...",
  "key_agreement_response": "base64_x3dh_response"
}

// Server forwards to initiator
```

#### e2ee:pre_keys_low
```javascript
// Server notifies when OTP supply < 10
{
  "remaining_pre_keys": 5,
  "should_upload": true
}
```

### Integration Checklist

- [ ] Run database migration to create E2EE tables
- [ ] Register routes in `app/main.py`:
  ```python
  from app.api.routes.e2ee import router as e2ee_router
  app.include_router(e2ee_router)
  ```
- [ ] Register socket handlers in `app/socket/__init__.py`:
  ```python
  from app.socket import e2ee_handlers  # Side effects: @sio.event decorators
  ```
- [ ] Client: Implement X3DH in libsignal or similar
- [ ] Client: Call `POST /e2ee/keys` on first launch (upload bundle)
- [ ] Client: Periodically fetch bundle with `GET /e2ee/keys/{peer_id}` before starting session
- [ ] Client: Implement Double Ratchet for ongoing encryption (client-side state)
- [ ] Client: Register session with `POST /e2ee/sessions` after X3DH
- [ ] Client: Listen on socket for `e2ee:pre_keys_low` and re-upload keys

---

## Whiteboard Module Integration

### Overview

Real-time collaborative drawing canvas:
- **Architecture:** Stroke-based (not bitmap), enabling undo/replay
- **Synchronization:** Socket.IO for real-time broadcasting
- **Participants:** In-memory tracking (cleared on disconnect)
- **Snapshots:** Efficient state transfer for late joiners
- **Undo/Redo:** Last-write-wins per user

### Database Schema

```python
# app/models/whiteboard.py

class WhiteboardSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Shared canvas within a channel."""
    channel_id: str (FK → channels)
    name: str
    created_by: str (FK → users)
    is_active: bool
    max_participants: int
    background_color: str (#rrggbb)
    width: int
    height: int

class WhiteboardStroke(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Single brush stroke (immutable)."""
    session_id: str (FK → WhiteboardSession)
    user_id: str (FK → users)
    tool: str (pen, eraser, line, rectangle, etc.)
    color: str (#rrggbb)
    width: float
    opacity: float (0.0-1.0)
    points: str (JSON [[x,y], [x,y], ...])
    z_index: int (layer ordering)

class WhiteboardSnapshot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Canvas state snapshot."""
    session_id: str (FK → WhiteboardSession)
    created_by: str (FK → users, nullable)
    snapshot_data: str (JSON or base64)
```

### REST Endpoints

#### 1. Create Whiteboard Session
```http
POST /whiteboards?channel_id=<channel_id>
Authorization: Bearer <token>

{
  "name": "Q1 Planning Board",
  "width": 1920,
  "height": 1080,
  "background_color": "#ffffff",
  "max_participants": 10
}

Response 201:
{
  "id": "wb_id",
  "channel_id": "ch_id",
  "name": "...",
  "created_by": "user_id",
  "is_active": true,
  "width": 1920,
  "height": 1080,
  "strokes": [],
  "created_at": "2026-04-09T10:00:00Z"
}
```

#### 2. Get Whiteboard (Full State)
```http
GET /whiteboards/{session_id}
Authorization: Bearer <token>

Response 200:
{
  "id": "wb_id",
  "strokes": [
    {
      "id": "stroke_id",
      "tool": "pen",
      "color": "#ff0000",
      "width": 3.5,
      "opacity": 1.0,
      "points": [[10,20], [15,25], ...],
      "z_index": 0,
      "user": {
        "id": "user_id",
        "username": "alice",
        "display_name": "Alice",
        "avatar_url": "..."
      },
      "created_at": "2026-04-09T10:05:00Z"
    },
    ...
  ],
  ...
}
```

**Use case:** Client joins whiteboard, fetches full state to render canvas.

#### 3. List Whiteboards in Channel
```http
GET /whiteboards/channel/{channel_id}?active_only=true&limit=50
Authorization: Bearer <token>

Response 200:
[
  {
    "id": "wb_id",
    "name": "Q1 Planning",
    "created_by": "user_id",
    "is_active": true,
    "stroke_count": 42,
    "participant_count": 3,
    "created_at": "2026-04-09T10:00:00Z"
  },
  ...
]
```

#### 4. Save Snapshot
```http
POST /whiteboards/{session_id}/snapshot
Authorization: Bearer <token>

{
  "snapshot_data": "base64_or_json_canvas_state"
}

Response 201:
{
  "id": "snap_id",
  "session_id": "wb_id",
  "created_by": "user_id",
  "snapshot_data": "...",
  "created_at": "2026-04-09T10:30:00Z"
}
```

**Optimization:** Save snapshots periodically to compress history (late joiners fetch snapshot + recent strokes instead of all strokes).

#### 5. Close Whiteboard
```http
DELETE /whiteboards/{session_id}
Authorization: Bearer <token>

Response 200:
{
  "status": "closed",
  "session_id": "wb_id",
  "message": "Whiteboard closed. History preserved."
}
```

**Note:** Only creator can close. Strokes remain in DB.

### Socket.IO Events

#### whiteboard:join
```javascript
// User joins whiteboard room
{
  "session_id": "wb_id",
  "username": "alice",
  "display_name": "Alice",
  "avatar_url": "https://..."
}

// Server response: participant list
{
  "participants": [
    {
      "user_id": "user_a",
      "username": "alice",
      "display_name": "Alice",
      "cursor_x": null,
      "cursor_y": null,
      "current_tool": null
    },
    ...
  ]
}

// Broadcast to others in room
{
  "user_id": "user_a",
  "username": "alice",
  "display_name": "Alice",
  "avatar_url": "..."
}
```

#### whiteboard:stroke
```javascript
// User draws a stroke
{
  "session_id": "wb_id",
  "tool": "pen",
  "color": "#ff0000",
  "width": 3.5,
  "opacity": 1.0,
  "points": [[10,20], [15,25], [20,30]],
  "z_index": 0
}

// Server broadcasts to room
{
  "stroke_id": "stroke_id",
  "user_id": "user_a",
  "tool": "pen",
  "color": "#ff0000",
  "width": 3.5,
  "opacity": 1.0,
  "points": [[10,20], [15,25], [20,30]],
  "z_index": 0,
  "created_at": "2026-04-09T10:05:00Z"
}
```

**Persistence:** Stroke persisted to DB before broadcast.

#### whiteboard:undo
```javascript
// User undoes their last stroke
{
  "session_id": "wb_id"
}

// Server broadcasts
{
  "stroke_id": "stroke_id",
  "user_id": "user_a"
}
```

**Logic:** Removes last stroke by that user (last-write-wins). No permission check (undo only affects own strokes).

#### whiteboard:clear
```javascript
// Session creator clears board
{
  "session_id": "wb_id"
}

// Server broadcasts
{
  "user_id": "user_a",
  "stroke_count": 42
}
```

**Security:** Only session creator allowed.

#### whiteboard:cursor_move
```javascript
// User moves cursor (low-frequency, e.g., every 100ms)
{
  "session_id": "wb_id",
  "cursor_x": 123.45,
  "cursor_y": 567.89
}

// Server broadcasts (except sender)
{
  "user_id": "user_a",
  "cursor_x": 123.45,
  "cursor_y": 567.89
}
```

**Use case:** Show remote user cursors for collaborative awareness.

#### whiteboard:tool_change
```javascript
// User switches tool
{
  "session_id": "wb_id",
  "tool": "eraser",
  "color": null,
  "width": 20
}

// Server broadcasts
{
  "user_id": "user_a",
  "tool": "eraser",
  "color": null,
  "width": 20
}
```

#### whiteboard:leave
```javascript
// User leaves whiteboard
{
  "session_id": "wb_id"
}

// Server broadcasts
{
  "user_id": "user_a"
}
```

### Integration Checklist

- [ ] Run database migration to create Whiteboard tables
- [ ] Register routes in `app/main.py`:
  ```python
  from app.api.routes.whiteboard import router as whiteboard_router
  app.include_router(whiteboard_router)
  ```
- [ ] Register socket handlers in `app/socket/__init__.py`:
  ```python
  from app.socket import whiteboard_handlers
  ```
- [ ] Client: On channel view, fetch whiteboard list with `GET /whiteboards/channel/{id}`
- [ ] Client: On whiteboard open, fetch full state with `GET /whiteboards/{id}`
- [ ] Client: Connect to socket room with `whiteboard:join` event
- [ ] Client: Listen for `whiteboard:stroke`, `whiteboard:undo`, `whiteboard:cursor_move`, etc.
- [ ] Client: Emit `whiteboard:stroke` as user draws
- [ ] Client: Implement canvas rendering from stroke array
- [ ] Client: Optional: Show remote cursors and tool info
- [ ] Client: Optional: Implement snapshot feature for efficient state sync

---

## Production Deployment Checklist

### Database
- [ ] Run Alembic migration to create all tables
- [ ] Ensure indexes on `user_id`, `session_id`, `is_active`
- [ ] Test concurrent access patterns (stripe race conditions)

### Performance
- [ ] E2EE: Monitor OTP consumption rate; auto-replenish when < 10
- [ ] Whiteboard: Implement snapshot cleanup (delete snapshots older than last 3)
- [ ] Whiteboard: Index on `(session_id, created_at)` for efficient stroke queries
- [ ] Socket.IO: Configure room max size or participant limits per whiteboard

### Security
- [ ] E2EE: Verify all keys validated as base64 (prevent injection)
- [ ] E2EE: Rate-limit key bundle uploads (max 1 per user per hour?)
- [ ] Whiteboard: Verify channel membership before allowing session creation
- [ ] Whiteboard: Log all destructive actions (clear, close)
- [ ] Socket: Auth token validation on every connect (inherited from socket.py)

### Monitoring
- [ ] E2EE: Track key rotation frequency, OTP consumption rate
- [ ] Whiteboard: Track concurrent participants per session, strokes per minute
- [ ] Socket: Monitor room membership, emit/broadcast latencies
- [ ] Errors: Log and alert on E2EE service exceptions, socket handler errors

### Documentation
- [ ] API: Add OpenAPI/Swagger docs for all E2EE and Whiteboard endpoints
- [ ] Socket: Document all event formats (done in code comments)
- [ ] Client: Provide example X3DH + Double Ratchet flow
- [ ] Client: Provide example whiteboard drawing implementation

---

## Key Design Decisions

### E2EE Architecture
1. **Server as keyserver only:** Plaintext never visible to server
2. **Atomic OTP consumption:** Database transaction ensures no reuse
3. **Signed pre-key retention:** Old versions kept for retransmission failures
4. **Session metadata:** Tracks initiator/responder + timestamps for audit

### Whiteboard Architecture
1. **Stroke-based, not bitmap:** Enables undo/replay, smaller bandwidth
2. **In-memory participants:** Cleared on disconnect (not persistent)
3. **Append-only strokes:** Immutable history for conflict-free merging
4. **Snapshot caching:** Compress history for efficient late-join
5. **Socket.IO rooms:** One room per whiteboard for message isolation

### Concurrency Model
- **E2EE:** Rely on database transactions (SQLAlchemy async)
- **Whiteboard:** Append-only + last-write-wins (no conflict resolution needed)
- **Participants:** In-memory dict (acceptable since ephemeral, cleared on disconnect)

---

## Future Enhancements

### E2EE
1. Implement offline session establishment (store session request in DB)
2. Add pre-key auto-replenishment (background task)
3. Implement key backup/recovery (encrypted backup code)
4. Support group E2EE (group sessions with multicast)

### Whiteboard
1. Text tool (collaborative text editing)
2. Sticky notes (persistent, per-user)
3. Shapes library (pre-drawn shapes, templates)
4. Infinite canvas (allow pan/zoom beyond bounds)
5. Live export (save as PNG/PDF)
6. Collaborative erasing (visible eraser cursor)
7. Layer support (hide/show/lock layers)

---

## File Reference

| File | Lines | Purpose |
|------|-------|---------|
| `app/models/e2ee_key.py` | 145 | ORM models for key storage |
| `app/schemas/e2ee.py` | 139 | Pydantic request/response schemas |
| `app/services/e2ee_service.py` | 405 | Business logic for key management |
| `app/api/routes/e2ee.py` | 283 | FastAPI endpoints |
| `app/socket/e2ee_handlers.py` | 252 | Socket.IO event handlers |
| `app/models/whiteboard.py` | 146 | ORM models for whiteboard |
| `app/schemas/whiteboard.py` | 246 | Pydantic schemas |
| `app/services/whiteboard_service.py` | 383 | Business logic for canvas |
| `app/api/routes/whiteboard.py` | 241 | FastAPI endpoints |
| `app/socket/whiteboard_handlers.py` | 501 | Socket.IO event handlers |
| **TOTAL** | **3,041** | **Production-grade implementation** |

---

## Questions?

- **E2EE:** See `app/services/e2ee_service.py` for atomic operations
- **Whiteboard:** See `app/services/whiteboard_service.py` for participant tracking
- **Socket events:** See docstrings in `e2ee_handlers.py` and `whiteboard_handlers.py`
- **API contracts:** See `app/schemas/` for full request/response specs
