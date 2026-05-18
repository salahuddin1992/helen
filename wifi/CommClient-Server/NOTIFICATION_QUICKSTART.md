# Notification System Quick Start

## 5-Minute Setup

The notification system is **already integrated** into the CommClient-Server codebase. No configuration needed.

### Files Created

```
app/models/notification.py              ← ORM Model
app/schemas/notification.py             ← Pydantic Schemas
app/services/notification_service.py    ← Business Logic
app/api/routes/notifications.py         ← REST Endpoints
app/socket/notification_handlers.py     ← Socket Events
```

Router and handlers **automatically registered** in:
- `app/api/routes/__init__.py` (includes notifications router)
- `app/socket/__init__.py` (imports handlers)
- `app/models/__init__.py` (Notification model registered)
- `app/models/user.py` (User.notifications relationship added)

### Start Server

```bash
cd /sessions/stoic-determined-tesla/mnt/wifi/CommClient-Server
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Tables auto-created on startup. Ready to use.

---

## Common Tasks

### 1. Emit a Real-Time Notification (Most Common)

In any service or socket handler:

```python
from app.socket.notification_handlers import create_and_emit_notification

# Create + emit in one call
await create_and_emit_notification(
    user_id="user_abc123",
    notification_type="message",
    title="New message from Alice",
    body="Hey, how are you?",
    reference_id="msg_xyz",
    reference_type="message",
)
```

**Example in chat_handlers.py:**

```python
@sio.event
async def chat_send_message(sid: str, data: dict):
    user_id = await get_user_id(sid)
    # ... send message to channel ...
    
    # Notify all other members
    for member in channel.members:
        if member.user_id != user_id:
            await create_and_emit_notification(
                user_id=member.user_id,
                notification_type="message",
                title=f"Message from {sender.display_name}",
                body=message.content[:100],
                reference_id=message.id,
                reference_type="message",
            )
```

### 2. List User's Notifications (REST)

```bash
curl -H "Authorization: Bearer <token>" \
  "http://localhost:8000/api/notifications?limit=50&offset=0"
```

Response:
```json
{
  "notifications": [...],
  "total": 42,
  "unread_count": 5
}
```

### 3. Get Unread Count (Lightweight)

```bash
curl -H "Authorization: Bearer <token>" \
  "http://localhost:8000/api/notifications/count"
```

Response:
```json
{
  "unread_count": 5
}
```

### 4. Mark as Read (REST)

```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"notification_ids": ["id1", "id2"]}' \
  "http://localhost:8000/api/notifications/mark-read"
```

### 5. Mark All as Read (REST)

```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  "http://localhost:8000/api/notifications/mark-all-read"
```

### 6. Delete a Notification (REST)

```bash
curl -X DELETE \
  -H "Authorization: Bearer <token>" \
  "http://localhost:8000/api/notifications/{notification_id}"
```

---

## Socket Events (Real-Time)

### Server → Client: Emit on New Notification

```javascript
// Client-side (JavaScript/React)
socket.on('notification:new', (data) => {
  console.log('New notification:', data);
  // {
  //   id: "abc123...",
  //   type: "message",
  //   title: "New message from Alice",
  //   body: "Hey, how are you?",
  //   reference_id: "msg_xyz",
  //   reference_type: "message",
  //   is_read: false,
  //   read_at: null,
  //   created_at: "2026-04-08T10:30:00Z"
  // }
});
```

### Client → Server: Mark as Read

```javascript
// Client marks notifications via socket
socket.emit('notification:mark_read', {
  notification_ids: ['id1', 'id2']
});

socket.on('notification:read_ack', (response) => {
  console.log('Marked read:', response);
  // {
  //   success: true,
  //   marked_count: 2,
  //   unread_count: 3
  // }
});
```

---

## Notification Types

| Type | Use Case |
|------|----------|
| `message` | New message in channel/DM |
| `call_incoming` | Incoming call |
| `call_missed` | Missed call |
| `contact_request` | User added you as contact |
| `group_invite` | Invited to group |
| `mention` | You were mentioned |
| `system` | System/admin announcements |

---

## Example: Add Message Notification

**Edit:** `app/socket/chat_handlers.py`

Find the `chat_send_message` function, add notification after message creation:

```python
@sio.event
async def chat_send_message(sid: str, data: dict):
    user_id = await get_user_id(sid)
    # ... existing code ...
    
    message = await MessageService.send_message(
        db, channel_id, user_id, content,
        msg_type=msg_type, reply_to=reply_to, file_id=file_id,
    )
    
    # ─── ADD THIS: Notify other members ───
    from app.socket.notification_handlers import create_and_emit_notification
    
    for member in channel.members:
        if member.user_id != user_id:
            try:
                await create_and_emit_notification(
                    user_id=member.user_id,
                    notification_type="message",
                    title=f"Message from {sender.display_name}",
                    body=message.content[:100],
                    reference_id=message.id,
                    reference_type="message",
                )
            except Exception as e:
                logger.error("notify_message_error", error=str(e))
    # ─── END ADD ───
    
    # ... rest of function ...
```

---

## Cleanup Old Notifications (Scheduled Task)

In a scheduled job or admin endpoint:

```python
from app.services.notification_service import notification_service
from app.db.session import async_session_factory

async def cleanup_old_notifications():
    async with async_session_factory() as db:
        deleted = await notification_service.delete_old(db, days=30)
        logger.info(f"Deleted {deleted} old notifications")
```

---

## Testing

Run a quick test:

```python
# test_notifications.py
import pytest
from app.services.notification_service import notification_service
from app.db.session import async_session_factory

@pytest.mark.asyncio
async def test_notification_workflow():
    async with async_session_factory() as db:
        # Create
        notif = await notification_service.create_notification(
            db,
            user_id="test_user",
            type="message",
            title="Test",
            body="Body",
        )
        assert notif.id
        assert not notif.is_read
        
        # Get unread
        unread = await notification_service.get_unread_count(db, "test_user")
        assert unread == 1
        
        # Mark read
        marked = await notification_service.mark_read(db, "test_user", [notif.id])
        assert marked == 1
        
        # Verify unread is now 0
        unread = await notification_service.get_unread_count(db, "test_user")
        assert unread == 0
```

Run:
```bash
pytest test_notifications.py -v
```

---

## Architecture

```
User sends message
       ↓
chat_send_message() event
       ↓
create_and_emit_notification()
       ├── notification_service.create_notification()  ← DB
       │   └── INSERT into notifications table
       │
       └── emit_notification()  ← Socket
           └── sio.emit('notification:new') to all user sids
               (in-memory, real-time)

Client receives 'notification:new' event ← Socket
       ↓
User clicks notification or polls /api/notifications ← REST
       ↓
mark_read() or mark_all_read()
       └── UPDATE notifications SET is_read=true, read_at=now
```

---

## Troubleshooting

**Q: Notification not appearing in real-time?**
- Check user is online: `presence_service.is_online(user_id)`
- Check error logs for `emit_notification_error`
- Notification is still saved in DB; clients can fetch via REST

**Q: "Notification not found" on delete/read?**
- User can only access their own notifications (security)
- Verify notification was created with correct `user_id`

**Q: Database error on startup?**
- Table created automatically on first run
- Check database connectivity in config
- Clear old tables if migrating: `DROP TABLE notifications CASCADE`

**Q: Missing imports?**
- All routers/handlers auto-registered
- Verify `from app.socket.notification_handlers import create_and_emit_notification` in your handler

---

## File Reference

| File | Purpose | Key Exports |
|------|---------|-------------|
| `notification.py` | ORM Model | `Notification` class |
| `notification.py` (schemas) | Pydantic | `NotificationResponse`, `NotificationListResponse`, `MarkReadRequest` |
| `notification_service.py` | Service Layer | `notification_service` singleton, methods: `create_notification`, `create_bulk`, `get_user_notifications`, `mark_read`, `mark_all_read`, `delete_notification`, `delete_old`, `get_unread_count` |
| `notifications.py` (routes) | REST API | 5 endpoints, auto-registered |
| `notification_handlers.py` | Socket Events | `notification_mark_read` event, `emit_notification()`, `create_and_emit_notification()` |

---

## Full Documentation

See **NOTIFICATIONS_INTEGRATION.md** for:
- Detailed integration patterns
- Real-world examples (messages, calls, contacts, mentions)
- Performance considerations
- Security details
- Advanced testing

See **NOTIFICATION_SYSTEM_README.md** for:
- Component details
- Database schema
- Configuration options
- Logging details

---

## Next Steps

1. ✅ Notification system ready to use
2. Start your server: `python -m uvicorn app.main:app --reload`
3. Integrate notifications into existing services (messages, calls, etc.)
4. Test with sample requests (curl commands above)
5. Deploy to production
