# `app.domains` — Domain-Oriented Re-Export Facade (Phase 4 / Module R)

This package provides a **clean, domain-oriented import surface** layered ON
TOP of the existing 617-file `app/` tree. No existing module was touched.

## Why

The historical layout (`app/api/routes/`, `app/services/`, `app/models/`,
`app/core/`, `app/socket/`, `app/monitoring/` ...) is technically organized
by **layer**, not by **domain**. New code paying the "find the right
service" tax for every import slowed down feature work. The facade collapses
that lookup into a single domain import:

```python
# Before (still works — facade is additive)
from app.api.routes.auth import router as auth_router
from app.core.security import create_access_token
from app.models.user import User
from app.services.auth_service import authenticate_user

# After
from app.domains import auth
auth.auth_router
auth.create_access_token
auth.User
auth.authenticate_user
```

## Domains

| Name         | Concern                                                |
|--------------|--------------------------------------------------------|
| `system`     | Config, audit, crypto, secrets, backup, monitoring     |
| `auth`       | Users, JWT, sessions, password hashing                 |
| `rbac`       | Roles, permissions, enforcer (Phase 2 Module G)        |
| `tenancy`    | Workspaces (Phase 3 Module M)                          |
| `messaging`  | Channels, messages, drafts, reactions                  |
| `files`      | Uploads (single + resumable), acceptance, file drop    |
| `calls`      | Signaling, SFU bridge, recording                       |
| `realtime`   | Socket.IO, transports, signal bus                      |
| `federation` | Inter-server federation, cluster mesh, gossip          |
| `oauth`      | External OAuth providers (Phase 3 Module N)            |
| `agents`     | Programmatic bots (Phase 3 Module L)                   |
| `pairing`    | Device pairing v1 + v2 (Phase 3 Module O)              |
| `admin`      | Admin-only routers                                     |

## Conventions

- **Safe imports only.** Every symbol is fetched through
  `_safe_import.safe_import()`. Missing modules raise nothing — they just
  vanish from the domain's `__all__`. This lets minimal deployments skip
  optional Phase-3 features without breaking the facade.
- **Router aliases.** Many `routes/*.py` modules export `router`; the
  facade re-exports them under unique aliases (`auth_router`,
  `messages_router`, ...) so multiple domains don't collide.
- **Module re-exports** (Socket.IO handlers, monitoring) are exposed as
  the whole namespace under their short name, e.g. `realtime.chat_handlers`.

## Introspection

```python
from app.domains._registry import build_registry, get_summary, find_symbol

reg = build_registry()             # {domain: {symbols, count, loaded, error}}
summary = get_summary()            # totals for admin dashboards
hits = find_symbol("create_access_token")   # → ["auth"]
```

## Versioning

`DOMAINS_VERSION` (`__init__.py`) follows additive-only semver. New symbol
= minor bump. Removed symbol = major bump (no removal so far).
