# Alembic Migration System

CommClient-Server uses **Alembic 1.13.1** for database schema versioning and migration management.

## Architecture

The migration system is designed for:
- **SQLite** (development, small deployments)
- **PostgreSQL** (production scale)
- **Async SQLAlchemy 2.0** (async/await engine and session patterns)
- **Both online and offline modes** (live migrations + SQL script generation)

### Files

```
migrations/
├── __init__.py
├── env.py              # Alembic environment config (offline/online modes)
├── script.py.mako      # Template for generated migration files
└── versions/
    ├── __init__.py
    └── 001_initial_schema.py  # Initial schema (all tables)
alembic.ini              # Alembic configuration (at project root)
```

## Configuration

**alembic.ini** — Main Alembic config at project root. Key settings:
- `script_location = migrations` — Points to migrations directory
- `sqlalchemy.url = sqlite:///commclient.db` — Placeholder (overridden by env.py)

**migrations/env.py** — Environment script that:
1. Imports `Base` and all models from `app.models`
2. Reads live database URL from `app.core.config.get_settings().db_url`
3. Supports both SQLite and PostgreSQL via conditional engine setup
4. Implements `run_migrations_offline()` for SQL script generation
5. Implements `run_migrations_online()` with async engine (asyncio.run)

**migrations/script.py.mako** — Jinja2 template for new migration files

## Initial Schema

**migrations/versions/001_initial_schema.py** creates all core tables:

| Table | Purpose |
|-------|---------|
| `users` | User accounts, profiles, status |
| `user_sessions` | Active JWT sessions per device (multi-device support) |
| `contacts` | Buddy list relationships, blocking, favorites |
| `channels` | DM (1-to-1) and group channels |
| `channel_members` | Channel membership with roles (admin, moderator, member) |
| `messages` | Text, file, image, system messages, replies |
| `reactions` | Emoji reactions on messages |
| `files` | File metadata, storage paths, checksums |
| `call_logs` | Audio/video/screen share call history |
| `message_receipts` | Per-recipient delivery and read tracking |

All tables include:
- UUID primary keys (32-char hex strings)
- `created_at` / `updated_at` with UTC timezone
- Appropriate foreign key constraints with CASCADE/SET NULL
- Indexes on frequently queried columns
- Unique constraints where applicable

## Usage

### View current migration state
```bash
alembic current
```

### Upgrade to latest migration
```bash
alembic upgrade head
```

### Generate SQL without applying
```bash
alembic upgrade head --sql
```

### Downgrade one migration
```bash
alembic downgrade -1
```

### Create a new migration (auto-detect changes)
```bash
alembic revision --autogenerate -m "add new_column to users"
```

### Create an empty migration (manual)
```bash
alembic revision -m "custom migration"
```

### View migration history
```bash
alembic history
```

## Environment Variables

The migration system reads from `app.core.config.get_settings()`:

- **DB_BACKEND** — `"sqlite"` (default) or `"postgresql"`
- **SQLITE_PATH** — Path to SQLite file (relative or absolute)
- **DATABASE_URL** — PostgreSQL connection string (if DB_BACKEND=postgresql)

Example for PostgreSQL:
```bash
export DB_BACKEND=postgresql
export DATABASE_URL=postgresql+asyncpg://user:password@localhost/commclient_db
```

Example for SQLite:
```bash
export DB_BACKEND=sqlite
export SQLITE_PATH=/data/commclient.db
```

## Online vs. Offline Modes

### Online Mode (Default)
```bash
alembic upgrade head
```
- Creates async SQLAlchemy engine
- Connects to database
- Executes migrations directly
- Fast, supports complex migrations

### Offline Mode
```bash
alembic upgrade head --sql
```
- Generates SQL without connecting
- Outputs SQL to stdout or file
- Use for code review, manual deployment, or CI/CD pipelines

Offline SQL output can be redirected:
```bash
alembic upgrade head --sql > migration.sql
```

## Model Discovery

The migration system automatically discovers models:

**migrations/env.py** imports all models:
```python
from app.models import (
    User,
    UserSession,
    Contact,
    Channel,
    ChannelMember,
    Message,
    Reaction,
    FileRecord,
    CallLog,
    MessageReceipt,
)
```

**Always import new models in env.py** when adding to `app.models/__init__.py`. Then run:
```bash
alembic revision --autogenerate -m "describe changes"
```

## Creating New Migrations

### 1. Modify models (e.g., add column to User)
```python
class User(Base, ...):
    new_column: Mapped[str] = mapped_column(String(64), nullable=True)
```

### 2. Auto-generate migration
```bash
alembic revision --autogenerate -m "add new_column to users"
```

### 3. Review generated migration
```
migrations/versions/002_add_new_column_to_users.py
```

### 4. Apply migration
```bash
alembic upgrade head
```

### 5. Test
```bash
# Verify new column exists
sqlite3 data/commclient.db "PRAGMA table_info(users);"
```

## Async Pattern

Migrations use SQLAlchemy 2.0 async patterns:

```python
async def run_migrations_online():
    connectable = create_async_engine(
        settings.db_url,
        echo=False,
        **engine_kwargs
    )
    async with connectable.begin() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()

asyncio.run(run_migrations_online())
```

This ensures proper async resource cleanup and works with:
- `aiosqlite` (SQLite)
- `asyncpg` (PostgreSQL)

## Common Tasks

### Reset database (development)
```bash
# Downgrade all migrations
alembic downgrade base

# Upgrade back to latest
alembic upgrade head
```

### Check if database is up-to-date
```bash
alembic current
alembic heads
```

### Generate migration without auto-detecting
```bash
alembic revision -m "manual migration"
# Edit versions/XXX_manual_migration.py
```

### Merge conflicting branches
```bash
alembic merge [rev1] [rev2] -m "merge revisions"
```

## Production Deployment

1. **Generate SQL offline**
   ```bash
   alembic upgrade head --sql > migrations_prod.sql
   ```

2. **Review SQL with DBA/team**

3. **Apply via your deployment tool** (Ansible, Terraform, manual SSH, etc.)
   ```bash
   # Via alembic
   alembic upgrade head
   
   # Or manually
   sqlite3 data/commclient.db < migrations_prod.sql
   ```

4. **Verify application starts**
   ```bash
   python run.py
   ```

## Troubleshooting

### `ModuleNotFoundError: No module named 'app'`
Ensure you're running from the project root:
```bash
cd /path/to/CommClient-Server
alembic upgrade head
```

### `OperationalError: database is locked` (SQLite)
SQLite has concurrency limits. For production, use PostgreSQL.

### Missing model in migration
Add the model import to `migrations/env.py`:
```python
from app.models.my_model import MyModel
```

Then regenerate:
```bash
alembic revision --autogenerate -m "add MyModel"
```

### Downgrade fails
Check the downgrade function in the migration file. Some operations (data loss) may not be reversible.

## Best Practices

1. **Auto-generate migrations when possible**
   ```bash
   alembic revision --autogenerate -m "descriptive name"
   ```

2. **Review generated migrations before applying**
   - Check for data loss
   - Verify indexes and constraints
   - Test on staging first

3. **Keep migrations small and focused**
   - One logical change per migration
   - Easier to debug and review

4. **Use descriptive names**
   - `add_avatar_url_to_users` ✓
   - `schema_update` ✗

5. **Test downgrade**
   ```bash
   alembic upgrade head
   alembic downgrade -1
   alembic upgrade head
   ```

6. **Never modify applied migrations**
   - Create a new migration instead
   - Old migrations are historical record

7. **For PostgreSQL, use asyncpg**
   - Uncomment `asyncpg==0.29.0` in requirements.txt
   - Set `DATABASE_URL=postgresql+asyncpg://...`
