# Mediasoup SFU Deployment

Helen-Server ships an embedded Node.js SFU worker (`sfu-worker/`) that
spawns automatically when topology promotes a group call from mesh to
SFU (currently triggered above `MESH_MAX_PARTICIPANTS=4`, env
overridable). This doc explains how the launcher locates the worker,
which env vars it reads, and how to deploy it in dev vs production.

## TL;DR

Pre-flight on every machine that runs Helen-Server in production:

1. Install **Node.js 18+** (`node --version` should report ≥ 18.19).
2. Make sure the firewall allows UDP **40000–49999** (RTC media) inbound
   from clients and TCP **4443** loopback (control plane).
3. Boot Helen-Server normally — the launcher does the rest.

That's it. No extra services, no Docker, no ConfigMaps. The launcher
runs `npm install --omit=dev` once on first boot (gated by
`COMMCLIENT_SFU_SKIP_INSTALL`), then keeps the worker alive under a
crash-restart supervisor.

---

## Architecture

```
┌──────────────┐  HTTP control (TCP 4443, MEDIASOUP_CONTROL_TOKEN)
│ Python server│ ◄────────────────────────────────────────────────┐
│ (FastAPI +   │                                                  │
│  Socket.IO)  │                                                  │
└──────────────┘                                                  │
        │                                              ┌──────────────┐
        │ spawn (subprocess)                           │ sfu-worker   │
        │ env: MEDIASOUP_CONTROL_PORT,                 │ (Node 18+,   │
        │      MEDIASOUP_CONTROL_TOKEN,                │  fastify +   │
        │      MEDIASOUP_RTC_MIN_PORT, ...             │  mediasoup)  │
        └─────────────────────────────────────────────►└──────────────┘
                                                             │
                                  UDP 40000-49999 (RTC media)│
                                                             ▼
                                                     ┌────────────┐
                                                     │  Clients   │
                                                     │  (browser/ │
                                                     │   Electron)│
                                                     └────────────┘
```

The **Python side** (`app/services/sfu_launcher.py`) is the supervisor:
spawns Node, restarts on crash, owns the control token, and exposes
`router_id`, `transport`, `producer`, `consumer` over HTTP to
`MediasoupBridge` (used by `topology_manager.allocate_router`).

The **Node side** (`sfu-worker/src/server.js`) owns the mediasoup C++
worker pool, ROUTER lifecycle, and PLAIN/RTCP-mux transports. It is
addressable only on `127.0.0.1:MEDIASOUP_CONTROL_PORT`. The control
plane is HMAC-token-authenticated; even on a misconfigured firewall,
the token gates every RPC.

---

## Environment variables

### Worker location & lifecycle

| Var | Default | Purpose |
|---|---|---|
| `COMMCLIENT_SFU_AUTOSTART_DISABLED` | unset | Set to `1` to keep the launcher idle (e.g. dev where you run Node manually). |
| `COMMCLIENT_SFU_EXTERNAL` | unset | `1` = expect an externally managed worker on `MEDIASOUP_CONTROL_HOST/PORT`. The launcher won't spawn anything; it just talks control HTTP. |
| `COMMCLIENT_SFU_SKIP_INSTALL` | unset | `1` = skip `npm install`. Use when `node_modules` is pre-staged via your image build. |
| `COMMCLIENT_SFU_DIR` | (auto) | Override the worker root. Default search order: `_MEIPASS/sfu-worker` (frozen) → repo `sfu-worker/` (dev). |
| `COMMCLIENT_NODE_BIN` | `node` | Path to the Node binary. |
| `COMMCLIENT_NPM_BIN` | `npm` | Path to npm. |

### Control plane

| Var | Default | Purpose |
|---|---|---|
| `MEDIASOUP_CONTROL_HOST` | `127.0.0.1` | Loopback by default — never expose this to the network. |
| `MEDIASOUP_CONTROL_PORT` | `4443` | TCP port for HTTP control RPC. |
| `MEDIASOUP_CONTROL_TOKEN` | `persistent_secrets` | HMAC key for control RPC auth. The launcher derives this from the same secret store the Python side reads, so the two stay in sync. |

### Media plane

| Var | Default | Purpose |
|---|---|---|
| `ICE_ANNOUNCED_IP` | (LAN IP) | Public IP advertised in SDP to clients. Override on multi-homed hosts or when behind a 1:1 NAT. |
| `MEDIASOUP_ANNOUNCED_IP` | = ICE_ANNOUNCED_IP | Mediasoup-specific override (rarely needed). |
| `MEDIASOUP_MIN_PORT` | `40000` | RTC port range floor (UDP). |
| `MEDIASOUP_MAX_PORT` | `49999` | RTC port range ceiling. Open this whole range in your firewall. |

### Recording (optional)

| Var | Default | Purpose |
|---|---|---|
| `MEDIASOUP_RECORDINGS_DIR` | `<data_dir>/recordings` | Where the worker stores call recordings. Auto-created. |

### Logging

| Var | Default | Purpose |
|---|---|---|
| `SFU_LOG_LEVEL` | `info` | Verbosity for the Node worker (debug, info, warn, error). |
| `LOG_DIR` | `<data_dir>/logs` | Worker stdout/stderr land in `sfu-worker.stdout.log` / `sfu-worker.stderr.log`. |

---

## Dev mode

Two options:

**A. Let the launcher manage the worker (recommended).** Just run
`python run.py`. On first boot the launcher runs `npm install` in
`sfu-worker/`. Subsequent boots skip the install (it checks
`node_modules/mediasoup/package.json`).

**B. Run the worker yourself.** Useful when iterating on
`sfu-worker/src/`:

```bash
# Terminal 1 — Node worker
cd CommClient-Server/sfu-worker
npm install
MEDIASOUP_CONTROL_TOKEN=$(cat ../data/.sfu_control_token) \
  MEDIASOUP_CONTROL_PORT=4443 \
  npm start

# Terminal 2 — Python server (skip auto-spawn)
cd CommClient-Server
COMMCLIENT_SFU_EXTERNAL=1 \
  MEDIASOUP_CONTROL_PORT=4443 \
  python run.py
```

---

## Production (frozen PyInstaller bundle)

The PyInstaller spec (`CommClient-Server.spec`) bundles the worker
**source** but NOT `node_modules`. Reasoning:

- The worker is plain JS (~10 KB).
- `node_modules` adds 42+ MB and contains the platform-specific
  mediasoup C++ binary — bundling it would tie the frozen exe to the
  build host's OS / glibc / libstdc++ minor version.
- The launcher runs `npm install` lazily on first SFU promotion, which
  takes ~30s on a typical operator machine.

Operators MUST have:

- **Node.js 18.19+** on PATH (`node --version`). Install via the
  official installer or your distro's package manager.
- **Network access** to npm registry on first run (or a pre-populated
  npm cache). After install, the worker runs offline.
- **UDP 40000–49999** open in the host firewall (the Helen
  `firewall_provision` module handles this on Windows automatically).

Skip the install step if you pre-stage `node_modules` (e.g. baked into
your VM/container image):

```bash
COMMCLIENT_SFU_SKIP_INSTALL=1 helen-server.exe
```

---

## Troubleshooting

### Worker never starts; logs say `sfu_npm_missing`

Node/npm not on PATH. Either install Node, or set
`COMMCLIENT_NODE_BIN=/full/path/to/node` and
`COMMCLIENT_NPM_BIN=/full/path/to/npm`.

### `sfu_worker_missing` at startup

The launcher couldn't find `sfu-worker/package.json`. Check:

1. Frozen build: did `_MEIPASS/sfu-worker/package.json` get bundled?
   The spec at `CommClient-Server.spec` bundles top-level files
   (`package.json`, `package-lock.json`, `README.md`) and `src/` —
   verify your spec is current.
2. Dev: is `CommClient-Server/sfu-worker/` checked into the repo?
3. Override: `COMMCLIENT_SFU_DIR=/abs/path/to/sfu-worker`.

### SFU promotion never happens

`MESH_MAX_PARTICIPANTS=4` means SFU triggers at 5+ participants. To
force earlier (e.g. for QA), set `HELEN_MESH_MAX_PARTICIPANTS=2` so a
3-person call already runs through SFU.

### Clients can't reach RTC ports

Worker is bound to all interfaces but `MEDIASOUP_ANNOUNCED_IP` may
advertise the wrong address (e.g. a docker bridge IP). Set
`ICE_ANNOUNCED_IP=<your-LAN-IP>` explicitly. Confirm with:

```bash
curl -s http://127.0.0.1:3000/api/ice-config -H 'Authorization: Bearer <jwt>' | jq
```

### Too many `sfu_router_alloc_failed` in server logs

The worker died and the launcher is restarting it faster than it can
allocate. Check `sfu-worker.stderr.log` for crashes (memory limits,
mediasoup version mismatch). The launcher uses exponential-backoff
restart so recurring crashes will eventually pause.

### Recordings dir filling up

`MEDIASOUP_RECORDINGS_DIR` defaults to `<data_dir>/recordings`. Mount a
larger volume there OR add a cron/scheduled-task to delete files older
than your retention window.

---

## Health-check command

```bash
curl -fsS http://127.0.0.1:4443/health \
  -H "Authorization: Bearer ${MEDIASOUP_CONTROL_TOKEN}" \
  | jq '{worker_pid, routers, transports}'
```

If this returns 200, the SFU plane is live. If it 404s, the worker
isn't running. If it 401s, the token is misconfigured (Python and Node
disagree).

---

## File layout in the frozen bundle

```
Helen-Server.exe
_internal/
  sfu-worker/
    package.json       ← bundled (~1 KB)
    package-lock.json  ← bundled (~30 KB)
    README.md
    src/
      server.js
      WorkerPool.js
      RouterRegistry.js
      config.js
    node_modules/      ← NOT bundled; created on first boot via npm install
```

The `node_modules` directory is created next to the frozen
`sfu-worker/`. On Windows that means inside the user's `_MEIPASS`
extraction dir or wherever `COMMCLIENT_SFU_DIR` points. Operators that
prefer a stable location can set `COMMCLIENT_SFU_DIR=C:\Helen\sfu-worker`
and stage the worker themselves.
