# Notification System Integration Guide

This document explains how to integrate the notification system into existing services (messages, calls, contacts, etc.) to emit real-time notifications to users.

## Architecture Overview

The notification system consists of:

1. **Model** (`app/models/notification.py`): SQLAlchemy ORM model storing notifications in the database
2. **Service** (`app/services/notification_service.py`): Async methods for CRUD operations and cleanup
3. **Schemas** (`app/schemas/notification.py`): Pydantic models for REST API and internal requests
4. **REST API** (`app/api/routes/notifications.py`): 5 endpoints for clients to fetch, mark read, and delete
5. **Socket Handlers** (`app/socket/notification_handlers.py`): Real-time event handlers + emission helpers

## REST API Endpoints

All endpoints require `Authorization: Bearer <token>` header.

### GET /api/notifications
List user's notifications with pagination and filtering.

**Query Parameters:**
- `limit` (int, 1-100): Results per page (default: 50)
- `offset` (int, >= 0): Results to skip (default: 0)
- `unread_only` (bool): Only return unread if true (default: false)

**Response:**
```json
{
  "notifications": [
    {
      "id": "abc123...",
      "type": "message",
      "title": "New message from Alice",
      "body": "Hey, how are you?",
      "reference_id": "msg_xyz...",
      "reference_type": "message",
      "is_read": false,
      "read_at": null,
      "created_at": "2026-04-08T10:30:00Z"
    }
  ],
  "total": 42,
  "unread_count": 5
}
```

### GET /api/notifications/count
Get unread notification count only (lightweight endpoint).

**Response:**
```json
{
  "unread_count": 5
}
```

### POST /api/notifications/mark-read
Mark specific notifications as read.

**Request Body:**
```json
{
  "notification_ids": ["id1", "id2", "id3"]
}
```

**Response:**
```json
{
  "marked_count": 3,
  "unread_count": 2
}
```

### POST /api/notifications/mark-all-read
Mark all unread notifications as read.

**Response:**
```json
{
  "marked_count": 5,
  "unread_count": 0
}
```

### DELETE /api/notifications/{notification_id}
Delete a single notification.

**Response:**
```json
{
  "deleted": true,
  "message": "Notification deleted successfully"
}
```

**Error (404):**
```json
{
  "detail": "Notification not found"
}
```

## Socket Events

### Server → Client: notification:new
Real-time notification delivery to connected sockets.

**Event Data:**
```json
{
  "id": "abc123...",
  "type": "message",
  "title": "New message from Alice",
  "body": "Hey, how are you?",
  "reference_id": "msg_xyz...",
  "reference_type": "message",
  "is_read": false,
  "read_at": null,
  "created_at": "2026-04-08T10:30:00Z"
}
```

### Client → Server: notification:mark_read
Client marks notifications as read via socket.

**Event Data:**
```json
{
  "notification_ids": ["id1", "id2"]
}
```

**Server Response → Client: notification:read_ack**
```json
{
  "success": true,
  "marked_count": 2,
  "unread_count": 3
}
```

## Integration Patterns

### 1. Create a Single Notification (Database Only)

Use when you need persistence but don't require real-time delivery. Typically for notifications of past events.

```python
from app.services.notification_service import notification_service

async def notify_user_somewhere(db: AsyncSession, user_id: str):
    notification = await notification_service.create_notification(
        db,
        user_id=user_id,
        type="message",
        title="New message from Bob",
        body="Hello, how are you?",
        reference_id=message_id,
        reference_type="message",
    )
    # notification.id, notification.created_at, etc. are now available
```

### 2. Create and Emit Real-Time Notification

Use this most common pattern: create notification AND emit to connected clients immediately.

```python
from app.socket.notification_handlers import create_and_emit_notification

# In a service or socket handler
notification_data, socket_count = await create_and_emit_notification(
    user_id="user_abc123",
    notification_type="message",
    title="New message from Bob",
    body="Hello, how are you?",
    reference_id=message_id,
    reference_type="message",
)
# notification_data: dict with all notification fields (ISO strings for datetimes)
# socket_count: how many connected sockets received it (0 if user offline)
```

### 3. Emit to Already-Created Notification

Use when notification already exists in the database, but you need to re-emit it (e.g., catch-up after reconnect).

```python
from app.socket.notification_handlers import emit_notification

notification_data = {
    "id": notif.id,
    "type": notif.type,
    "title": notif.title,
    "body": notif.body,
    "reference_id": notif.reference_id,
    "reference_type": notif.reference_type,
    "is_read": notif.is_read,
    "read_at": notif.read_at.isoformat() if notif.read_at else None,
    "created_at": notif.created_at.isoformat(),
}
emitted_count = await emit_notification(user_id, notification_data)
```

### 4. Bulk Notifications (Group/Channel)

Create notifications for multiple users (e.g., group message, channel invite).

```python
from app.services.notification_service import notification_service
from app.socket.notification_handlers import emit_notification

async with async_session_factory() as db:
    # Create for all members
    recipient_ids = [m.user_id for m in channel.members if m.user_id != sender_id]
    notifications = await notification_service.create_bulk(
        db,
        user_ids=recipient_ids,
        type="message",
        title=f"New message in #{channel.name}",
        body=message.content,
        reference_id=message.id,
        reference_type="message",
    )
    
    # Emit to each user's connections
    for notif in notifications:
        notification_data = {
            "id": notif.id,
            "type": notif.type,
            "title": notif.title,
            "body": notif.body,
            "reference_id": notif.reference_id,
            "reference_type": notif.reference_type,
            "is_read": notif.is_read,
            "read_at": notif.read_at.isoformat() if notif.read_at else None,
            "created_at": notif.created_at.isoformat(),
        }
        await emit_notification(notif.user_id, notification_data)
```

## Example Integrations

### Message Notification (chat_handlers.py)

When a message is sent in a channel, notify all channel members:

```python
from app.socket.notification_handlers import create_and_emit_notification

@sio.event
async def chat_send_message(sid: str, data: dict):
    user_id = await get_user_id(sid)
    # ... create message ...
    
    # Notify channel members (except sender)
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
                logger.error("failed_to_emit_message_notification", error=str(e))
```

### Call Incoming Notification (call_handlers.py)

When a call is initiated, notify the recipient:

```python
from app.socket.notification_handlers import create_and_emit_notification

@sio.event
async def call_initiate(sid: str, data: dict):
    # ... create call ...
    
    # Notify recipient
    await create_and_emit_notification(
        user_id=recipient_user_id,
        notification_type="call_incoming",
        title=f"Incoming call from {initiator.display_name}",
        reference_id=call.id,
        reference_type="call",
    )
```

### Call Missed Notification (call_service.py)

When a call expires without being answered:

```python
from app.socket.notification_handlers import create_and_emit_notification

async def _call_timeout_expired(self, call_id: str):
    call = self.get_call(call_id)
    if call and call.status == "ringing":
        # Notify initiator of missed call
        await create_and_emit_notification(
            user_id=call.initiator_id,
            notification_type="call_missed",
            title=f"Missed call from {call.recipient.display_name}",
            reference_id=call.id,
            reference_type="call",
        )
```

### Contact Request Notification (user_service.py or contact flow)

When a user adds another as a contact:

```python
from app.socket.notification_handlers import create_and_emit_notification

# After contact is created
await create_and_emit_notification(
    user_id=contact_id,
    notification_type="contact_request",
    title=f"{requester.display_name} added you as a contact",
    reference_id=contact.id,
    reference_type="contact",
)
```

### Group/Channel Invite Notification (channel_service.py)

When a user is invited to a group:

```python
from app.socket.notification_handlers import create_and_emit_notification

for invitee_id in invitee_ids:
    await create_and_emit_notification(
        user_id=invitee_id,
        notification_type="group_invite",
        title=f"Invited to {channel.name}",
        body=f"Join our group chat",
        reference_id=channel.id,
        reference_type="channel",
    )
```

### Mention Notification (message_service.py)

When a user is mentioned in a message:

```python
import re
from app.socket.notification_handlers import create_and_emit_notification

# Parse mentions from message content (e.g., @username)
mention_pattern = r"@(\w+)"
mentions = re.findall(mention_pattern, message.content)

for username in mentions:
    mentioned_user = await UserService.get_user_by_username(db, username)
    await create_and_emit_notification(
        user_id=mentioned_user.id,
        notification_type="mention",
        title=f"{sender.display_name} mentioned you",
        body=message.content,
        reference_id=message.id,
        reference_type="message",
    )
```

## Error Handling

All notification functions handle errors gracefully and log them:

```python
try:
    notification_data, emitted = await create_and_emit_notification(...)
    logger.info(f"Notification emitted to {emitted} sockets")
except Exception as e:
    logger.error("notification_emission_failed", error=str(e))
    # Continue — notification is persisted in DB even if emission fails
```

The pattern ensures:
- Database operations fail fast and are logged
- Socket emission failures don't break the main flow
- User gets notification on next sync/poll if real-time fails

## Notification Types Reference

Standardized notification types used across the system:

| Type | Use Case | Reference |
|------|----------|-----------|
| `message` | New message in channel/DM | message_id |
| `call_incoming` | Incoming call | call_id |
| `call_missed` | Missed call | call_id |
| `contact_request` | User added you as contact | contact_id |
| `group_invite` | You were invited to group | channel_id |
| `system` | Administrative messages | N/A |
| `mention` | User mentioned you | message_id |

## Cleanup

Old read notifications can be cleaned up periodically:

```python
from app.services.notification_service import notification_service

# In a maintenance task or scheduled job
async with async_session_factory() as db:
    deleted = await notification_service.delete_old(
        db,
        days=30,  # Delete read notifications older than 30 days
    )
    logger.info(f"Cleaned up {deleted} old notifications")
```

## Testing

Example test case for notification emission:

```python
import pytest
from app.socket.notification_handlers import emit_notification
from app.services.notification_service import notification_service
from app.db.session import async_session_factory

@pytest.mark.asyncio
async def test_notification_creation_and_emission():
    async with async_session_factory() as db:
        # Create notification
        notif = await notification_service.create_notification(
            db,
            user_id="test_user",
            type="message",
            title="Test notification",
            body="This is a test",
        )
        
        assert notif.id
        assert notif.type == "message"
        assert not notif.is_read
        
        # Verify persistence
        unread = await notification_service.get_unread_count(db, "test_user")
        assert unread == 1
        
        # Mark as read
        marked = await notification_service.mark_read(db, "test_user", [notif.id])
        assert marked == 1
```

## Performance Considerations

- Notifications are indexed on `(user_id, created_at)` and `(user_id, is_read)` for fast queries
- Bulk creation uses `db.add_all()` for efficiency
- Socket emission happens asynchronously in background
- Old read notifications are cleaned up periodically to prevent DB bloat
- Unread count is computed on-demand; consider caching in high-volume scenarios

## Security

- All REST endpoints require authentication via Bearer token
- Notifications are user-scoped: users can only access/delete their own
- Socket events are authenticated at connection time
- Reference IDs are strings; validation depends on the referenced entity type
