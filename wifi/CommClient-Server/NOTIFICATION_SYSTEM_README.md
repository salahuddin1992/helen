# Notification System for CommClient-Server

Complete production-grade notification system for the FastAPI+Socket.IO backend, enabling real-time and persistent notification delivery to users.

## Files Created

```
app/
├── models/
│   └── notification.py              # SQLAlchemy ORM model (Notification)
├── schemas/
│   └── notification.py              # Pydantic schemas for REST API
├── services/
│   └── notification_service.py      # Async service with CRUD + bulk ops
├── api/
│   └── routes/
│       └── notifications.py         # 5 REST endpoints (GET, POST, DELETE)
└── socket/
    └── notification_handlers.py     # Socket event handlers + emit helpers
```

## Key Components

### 1. Notification Model (`app/models/notification.py`)

SQLAlchemy ORM model with:
- **Primary Key**: `id` (UUID hex string, 32 chars)
- **Foreign Key**: `user_id` → `users.id` (indexed, cascading delete)
- **Type**: String(32) — standardized types: message, call_incoming, call_missed, contact_request, group_invite, system, mention
- **Content**: `title` (256 chars) + optional `body` (Text)
- **References**: `reference_id` and `reference_type` for linking to related entities
- **Read State**: `is_read` (bool) + `read_at` (datetime, nullable)
- **Timestamps**: `created_at`, `updated_at` (TimestampMixin)
- **Indexes**: `(user_id, created_at)` and `(user_id, is_read)` for fast queries

Relationship: Notification.user → User (with cascade delete)

### 2. Notification Service (`app/services/notification_service.py`)

Async service class with:

**Single Notification:**
- `create_notification()` — Create one notification, persist to DB
- `get_user_notifications()` — Fetch with pagination, optional unread filter → tuple(list, total, unread_count)
- `get_unread_count()` — Quick count of unread

**Bulk Operations:**
- `create_bulk()` — Create for multiple users (group notifications)
- `mark_read()` — Mark specific notifications read, return count
- `mark_all_read()` — Mark all unread as read
- `delete_notification()` — Remove one, with ownership verification
- `delete_old()` — Cleanup read notifications older than N days

All methods use `AsyncSession` with proper transaction handling and structured logging.

Singleton: `notification_service = NotificationService()`

### 3. Schemas (`app/schemas/notification.py`)

Pydantic v2 models:
- `NotificationResponse` — Single notification (REST API response)
- `NotificationListResponse` — Paginated list with metadata (total, unread_count)
- `NotificationCreate` — Internal schema for service layer
- `MarkReadRequest` — REST request body for marking read

### 4. REST API Routes (`app/api/routes/notifications.py`)

5 endpoints, all require `Authorization: Bearer <token>`:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/notifications` | List with pagination & filtering |
| GET | `/api/notifications/count` | Unread count only (lightweight) |
| POST | `/api/notifications/mark-read` | Mark specific as read |
| POST | `/api/notifications/mark-all-read` | Mark all as read |
| DELETE | `/api/notifications/{id}` | Delete notification |

All return structured responses with HTTP status codes.

### 5. Socket Handlers (`app/socket/notification_handlers.py`)

**Event Handlers:**
- `notification:mark_read` — Client marks via socket (alternative to REST)

**Emission Helpers:**
- `emit_notification(user_id, notification_data)` — Emit to all user's connected sockets
- `create_and_emit_notification(...)` — Create + emit in one call (most common pattern)

**Real-Time Event:**
- Server → Client: `notification:new` — Broadcasts new notification to all user sockets

## Integration Points

### Router Registration

Notification router automatically registered in `app/api/routes/__init__.py`:
```python
from app.api.routes.notifications import router as notifications_router
api_router.include_router(notifications_router)
```

### Socket Handler Registration

Notification handlers imported in `app/socket/__init__.py`:
```python
from app.socket.notification_handlers import *
```

Both files already updated.

### User Model

User model updated with notification relationship:
```python
notifications: Mapped[list["Notification"]] = relationship(
    "Notification", back_populates="user", cascade="all, delete-orphan",
)
```

### Models __init__

Notification imported in `app/models/__init__.py` for SQLAlchemy discovery.

## Database Migration

No explicit migration needed. On startup, the app creates the `notifications` table via:
```python
async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)
```

Table schema:
```sql
CREATE TABLE notifications (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32) NOT NULL,
    type VARCHAR(32) NOT NULL,
    title VARCHAR(256) NOT NULL,
    body TEXT,
    reference_id VARCHAR(32),
    reference_type VARCHAR(32),
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    read_at TIMESTAMP WITH TIMEZONE,
    created_at TIMESTAMP WITH TIMEZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIMEZONE NOT NULL DEFAULT NOW(),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_user_id_created_at (user_id, created_at),
    INDEX idx_user_id_is_read (user_id, is_read)
);
```

## Usage Examples

### Create & Emit Real-Time (Most Common)

```python
from app.socket.notification_handlers import create_and_emit_notification

# In chat_handlers.py when message is sent
await create_and_emit_notification(
    user_id=recipient_id,
    notification_type="message",
    title=f"Message from {sender.display_name}",
    body=message.content[:100],
    reference_id=message.id,
    reference_type="message",
)
```

### Create Without Emission (Async Persistence)

```python
from app.services.notification_service import notification_service
from app.db.session import async_session_factory

async with async_session_factory() as db:
    notification = await notification_service.create_notification(
        db,
        user_id="user_abc123",
        type="system",
        title="Maintenance scheduled",
        body="Server will restart at 2 AM UTC",
    )
```

### Query Notifications

```python
# In a route or service
notifications, total, unread_count = await notification_service.get_user_notifications(
    db,
    user_id=user_id,
    limit=50,
    offset=0,
    unread_only=False,
)
```

### Bulk Create (Group Notification)

```python
notifications = await notification_service.create_bulk(
    db,
    user_ids=["user1", "user2", "user3"],
    type="group_invite",
    title="Invited to Design Group",
    reference_id=channel_id,
    reference_type="channel",
)
```

## Testing

Example pytest test:

```python
@pytest.mark.asyncio
async def test_create_notification():
    from app.services.notification_service import notification_service
    from app.db.session import async_session_factory
    
    async with async_session_factory() as db:
        notif = await notification_service.create_notification(
            db,
            user_id="test_user",
            type="message",
            title="Test",
            body="Body",
        )
        
        assert notif.id
        assert not notif.is_read
        
        unread = await notification_service.get_unread_count(db, "test_user")
        assert unread == 1
        
        await notification_service.mark_read(db, "test_user", [notif.id])
        unread = await notification_service.get_unread_count(db, "test_user")
        assert unread == 0
```

## Performance Notes

- **Indexes**: `(user_id, created_at)` DESC for list queries, `(user_id, is_read)` for unread counts
- **Bulk Insert**: Uses `db.add_all()` for efficiency on group notifications
- **Socket Emission**: Non-blocking async operations; failures logged but don't block DB commit
- **Cleanup**: Periodically delete old read notifications to prevent DB bloat
  ```python
  await notification_service.delete_old(db, days=30)
  ```

## Security

- **Authentication**: All REST endpoints require Bearer token (HTTPBearer)
- **Ownership**: Notifications are user-scoped; users can only access their own
- **Validation**: Type and reference_type validate against allowed values
- **Cascading Delete**: Notifications cascade-delete with user for data consistency
- **SQL Injection**: SQLAlchemy parameterized queries prevent injection

## Logging

All operations logged via structlog:
- `notification_created` — New notification
- `notifications_bulk_created` — Bulk creation
- `notifications_fetched` — List query
- `notifications_marked_read` — Mark operations
- `notification_deleted` — Deletion
- `old_notifications_deleted` — Cleanup
- `notification_emitted` — Socket emission with socket count

## Integration Documentation

See **NOTIFICATIONS_INTEGRATION.md** for:
- Detailed usage patterns
- Example integrations with chat, calls, contacts
- Error handling best practices
- Notification type reference
- Testing examples

## Standup Summary

✅ **Delivered:**
1. Production-grade ORM model with proper indexes and relationships
2. Complete async service layer with CRUD + bulk + cleanup
3. Pydantic schemas for REST and internal use
4. 5 REST endpoints with pagination, filtering, and error handling
5. Socket.IO real-time event handlers + emission helpers
6. Proper registration in app routers and socket handlers
7. 400+ lines of integration documentation with real-world examples
8. Follows all codebase patterns: async, structlog, UUIDPrimaryKeyMixin, TimestampMixin, SQLAlchemy async
9. Production security: authentication, ownership verification, validation
10. Database-ready: auto-created tables on startup with proper constraints

All files follow the exact patterns from the existing codebase (auth, messages, calls, users services).
