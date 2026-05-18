# Helen-Shell

A single Electron binary that wraps any Helen web panel as a desktop app.

## Why

Until now, Helen had **one** Electron app (`CommClient-Desktop`) and
several web-only panels (Admin, Secret Admin, Vault, iOS-Mobile sim,
iOS-Admin sim). Operators who wanted those as standalone desktop windows
had to keep Chrome open. Helen-Shell replaces that — every panel is now
launchable as either:

  - A **desktop app** (Electron, this folder)
  - A **web page** (already served by Helen-Server)

## Install

```bash
cd Helen-Shell
npm install
```

## Run

```bash
# By panel ID (recommended)
npm run admin
npm run secret-admin
npm run vault
npm run ios
npm run admin-mobile
npm run hub

# Or pass any --url= directly
npx electron . --url=http://127.0.0.1:3088/admin/ --title="Helen Admin"

# DevTools when troubleshooting
HELEN_SHELL_DEVTOOLS=1 npm run admin
```

## Add a new panel

1. Mount the static folder in `CommClient-Server/app/main.py` (e.g. `/foo`).
2. Add an entry to `apps.json`:
   ```json
   { "id": "foo", "title": "Helen Foo", "path": "/foo/",
     "width": 1100, "height": 800 }
   ```
3. Optionally add `"foo": "electron . --id=foo"` to `package.json` scripts.

That's it — the same binary now opens it.

## Web equivalents

Each panel is also reachable directly in a browser:

| Panel              | URL                                        |
|--------------------|--------------------------------------------|
| Hub                | `http://127.0.0.1:3088/hub/`               |
| Admin              | `http://127.0.0.1:3088/admin/`             |
| Secret Admin       | `http://127.0.0.1:3088/admin-secret/`      |
| Vault              | `http://127.0.0.1:3088/vault/`             |
| iOS Sim            | `http://127.0.0.1:3088/mobile/`            |
| iOS-Admin Sim      | `http://127.0.0.1:3088/admin-mobile/`      |
| Desktop (web)      | `http://127.0.0.1:3088/desktop/`           |

## Architecture

```
+---------------------+
|   Helen-Server      |  ← single backend on :3088
|   (Python/FastAPI)  |
+---------------------+
          |
          | static + WebSocket
          v
+---------------------+      +--------------------+
| Browser (any UI)    |  OR  | Helen-Shell        |
|                     |      | (Electron, this)   |
+---------------------+      +--------------------+
                                 ^
                                 | --id=admin / --id=vault / ...
                                 |
                              one binary, many roles
```
