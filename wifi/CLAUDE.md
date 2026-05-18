# Helen — project-level instructions for AI assistants

> The user calls this **Helen** (not "CommClient" — that's the legacy
> name still on disk). Branding inside `app/` and the desktop UI uses
> "Helen" everywhere. Treat the two names as synonyms.

## Hard rules

1. **100 % LAN-only / private intranet.** No FCM, no APNs, no
   Cloudflare/AWS/Azure/GCP, no internet-facing services at runtime.
   GitHub Actions for *building* iOS/macOS is allowed; *runtime*
   never reaches the public internet.
2. **No questions for the user.** Make the call yourself, run it,
   and report the result. Asking permission for routine work
   actively annoys this user.
3. **Always kill stale Helen-Server before smoke tests.**
   PyInstaller-frozen binaries can outlive the wrapper they were
   spawned from, return 404 for new routes, and waste hours of
   debugging. `pkill -f Helen-Server` (Linux/WSL) or
   `taskkill //F //IM Helen-Server.exe` (Windows) before any curl.
4. **Don't commit anything unless explicitly asked.** Stage,
   describe what changed, wait for the OK.

## Layout

```
C:/Users/youse/c/wifi/
├── CommClient-Server/      Helen-Server (FastAPI + Socket.IO, PyInstaller)
├── Helen-Router/           Helen-Router (mandatory entry point + mesh)
├── CommClient-Desktop/     Helen Desktop (Electron + React + Vite)
├── CommClient-Mobile/      Helen Mobile (Capacitor wraps Desktop renderer)
├── Helen-Rendezvous/       NAT traversal between LAN subnets
├── iOS/                    Swift sources (Mac-only build)
├── Vault/web/              Static panel mounted by the server
├── DELIVERY-MANIFEST.md    Living doc — append a section per session
└── CLAUDE.md               This file.
```

## Build commands

| Target | Command | Cwd |
|---|---|---|
| Helen-Server (Win) | `./venv/Scripts/python -m PyInstaller CommClient-Server.spec --noconfirm --clean` | `CommClient-Server/` |
| Helen-Server (Linux) | `wsl -d Ubuntu-22.04 -u root -- bash /mnt/c/Users/youse/c/wifi/CommClient-Server/scripts/build-linux-wsl.sh` | anywhere |
| Helen-Router (Win) | `../CommClient-Server/venv/Scripts/python -m PyInstaller Helen-Router.spec --noconfirm --clean` | `Helen-Router/` |
| Helen-Router (Linux) | `wsl -d Ubuntu-22.04 -u root -- bash /mnt/c/Users/youse/c/wifi/Helen-Router/scripts/build-linux-wsl.sh` | anywhere |
| Helen Desktop | `npm run build:renderer && npx electron-builder --win` | `CommClient-Desktop/` |
| Helen Mobile | `node scripts/sync-renderer.mjs && npx cap sync android && cd android && ./gradlew assembleDebug assembleRelease bundleRelease` | `CommClient-Mobile/` |
| Helen-Router NSIS installer | `"/c/Program Files (x86)/NSIS/makensis.exe" installer.nsi` | `Helen-Router/` |
| Sign all 8 binaries | `pwsh tools/self-sign-helen.ps1` | `CommClient-Server/` |
| Run all backend tests | `JWT_SECRET=$(./venv/Scripts/python -c 'import secrets;print(secrets.token_hex(32))') ./venv/Scripts/python -m pytest tests/` | `CommClient-Server/` |

## WSL gotchas

- Always prefix with `MSYS2_ARG_CONV_EXCL='*'` so Git Bash doesn't
  rewrite `/mnt/c/...` to a Windows path. Without it, every
  `wsl -d Ubuntu-22.04 -- bash /mnt/c/...` fails with
  "No such file or directory".
- WSL Ubuntu has **no passwordless sudo**. Use `wsl -u root` and
  drop `sudo` from your scripts; do not try `sudo -S`.
- The shared venv `/tmp/helen-linux-venv` is reused across
  Helen-Server and Helen-Router builds.

## Self-signing certificate

- Thumbprint: **`A685150F02C4E48DD435A191E31BC81382C42304`**
- Subject: `CN=Helen Project Internal`
- Algorithm: RSA 4096, SHA-256, valid until 2036-05-04
- Stored in `Cert:\CurrentUser\My` (NOT exported).
- `tools/self-sign-helen.ps1` is the canonical signer. To make
  Windows trust it silently across the LAN, rerun with
  `-ImportToTrustedRoot $true`.

## Lifespan startup events to expect in logs

A correctly-built Helen-Server prints (structlog JSON):

```
crash_reporter_installed
audit_chain_configured
call_orchestrators_wired
lan_push_manager_configured
calendar_reminder_worker_started
```

If any of these are missing in a fresh-install smoke test, the
binary you're testing is older than the source tree.

## Helen-Router tokens to refuse

Mirror the `_WEAK_TOKENS` set in `Helen-Router/app/main.py` —
the deployment will refuse to start with any of these:
- `0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9` (old NSIS fallback)
- `REPLACE_ME_BEFORE_RUNNING_HELEN_ROUTER_64_chars_long_xxxxxxxxxx`
- `change-me`, `changeme`, `secret`

Same idea for Helen-Server's `_WEAK_JWT_SECRETS` set in `app/main.py`.

## Useful endpoints (auth-gated unless noted)

| Path | Purpose |
|---|---|
| `/api/health` | public — load-balancer probe |
| `/api/calendar/events` | CRUD for internal calendar |
| `/api/calendar/feed.ics` | per-user iCal feed |
| `/api/transcripts/*` | whisper.cpp wrapper |
| `/api/admin/crashes` | local SQLite crash log |
| `/api/admin/audit-chain/{head,verify,entries}` | tamper-evident chain |
| `/router/*` | Helen-Router admin (token-gated) |
| `/mesh/{lsa,topology,path/{sid},neighbours}` | Helen-Router mesh overlay |

## Optional transport backends

Helen-Server's runtime is broker- and federation-agnostic. The
defaults are Redis Streams (broker) + HMAC-JSON-over-HTTP
(federation), but operators can swap in any of these via env vars:

| Env var | Values | Effect |
|---|---|---|
| `HELEN_BROKER_BACKEND` | `redis` (default), `nats`, `mqtt`, `zeromq`, `rabbitmq` | Inter-server pub/sub backend |
| `HELEN_NATS_URL` | `nats://10.0.0.10:4222` | Required when backend=nats |
| `HELEN_MQTT_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` / `_TLS` | LAN MQTT broker | Required when backend=mqtt |
| `HELEN_ZEROMQ_BIND` | `tcp://0.0.0.0:5555` | ZMQ PUB bind URL when backend=zeromq |
| `HELEN_ZEROMQ_PEERS` | `tcp://10.0.0.6:5555,tcp://10.0.0.7:5555` | CSV of peer ZMQ PUB URLs |
| `HELEN_RABBITMQ_URL` | `amqp://user:pass@10.0.0.5:5672/` | Required when backend=rabbitmq |
| `HELEN_RABBITMQ_EXCHANGE` | `helen.events` | Topic exchange name |
| `HELEN_SSH_TUNNELS_ENABLED` | `1` | Enable SSH tunnel manager |
| `HELEN_SSH_TUNNELS` | CSV: `local:user@host:22:13000:peer:3000` | Tunnel specs |
| `HELEN_FEDERATION_BACKEND` | `http` (default), `grpc` | Inter-server RPC layer |
| `HELEN_GRPC_FEDERATION_PORT` | `50051` | gRPC listener port |
| `HELEN_GRPC_FEDERATION_HOST` | `0.0.0.0` | gRPC bind host |
| `HELEN_VPN_BACKEND` | empty (default), `wireguard` | Encrypts inter-server traffic |
| `HELEN_WG_LISTEN_PORT` | `51820` | WireGuard UDP port |
| `HELEN_WG_MESH_SUBNET` | `10.99.0.0/24` | Internal /24 for WG mesh |
| `HELEN_WG_OVERRIDE_IP` | empty | Skip deterministic-IP hash |
| `HELEN_MESH_TOPOLOGY` (Helen-Router) | `mesh` (default), `ring` | Routing strategy |
| `HELEN_WAN_PORTMAP_ENABLED` | `1` | Run the WAN port-forward manager |
| `HELEN_WAN_EXTERNAL_PORT` / `_INTERNAL_PORT` / `_PROTOCOL` | `3000` / `3000` / `TCP` | Port to forward |
| `HELEN_WAN_UPNP_URL` | SSDP device-description URL | Skip discovery; map via this IGD |
| `HELEN_WAN_REFRESH_S` | `3600` | UPnP re-assert interval |
| `HELEN_WAN_VENDOR` | `Mikrotik` / `Ubiquiti` / `OpenWrt` / `pfSense` / `Cisco` / `Generic` | Picks the manual-instructions template |
| `HELEN_WAN_PEER_PROBES` | CSV of `https://10.0.0.6:3000` | Peers asked to probe-back our external IP |
| `HELEN_TURN_SECRET` | hex string | Drives TURN allocate self-test in `/api/admin/transports/turn/health` |
| `HELEN_DNS_BLOCKLIST` (Helen-Router) | path to hosts-format file | Pi-hole-style filtering |
| `HELEN_DNS_UPSTREAMS` (Helen-Router) | CSV: `9.9.9.9:53,1.1.1.1:53` | Multi-upstream forwarder fallback chain |
| `HELEN_DNS_CACHE_MAX` (Helen-Router) | `1024` | LRU cache size for upstream answers |
| `HELEN_STUN_LISTEN` | `0.0.0.0:3478` | Self-hosted STUN binding responder bind address |
| `HELEN_FEDERATION_AUTODISCOVER` | `1` | mDNS discover sibling Helen federation endpoints |
| `HELEN_FEDERATION_CLUSTER_ID` | `default` | Only auto-add candidates with matching cluster_id |
| `HELEN_FEDERATION_ADVERTISE_HOST` | empty | Override the IPv4 we advertise via mDNS |
| `HELEN_BACKUP_VERIFY_ENABLED` | `1` | Periodic restore-into-tempdir backup checks |
| `HELEN_BACKUP_VERIFY_INTERVAL_S` | `86400` | Seconds between verifications |
| `HELEN_BACKUP_VERIFY_REQUIRED_TABLES` | `users,messages` | Tables the verifier expects to find |
| `HELEN_FEDERATION_BPS_LIMIT` | `0` (off) | Per-peer bytes/sec cap on federation traffic |
| `HELEN_FEDERATION_BURST_BYTES` | `4 × limit` | Token-bucket capacity |
| `HELEN_FEDERATION_SHAPER_MAX_WAIT_S` | `30` | Max in-call sleep before raising ShaperOverloaded |
| `HELEN_ONLINE_MODE_DEFAULT` | `off` (default) / `on` | Initial state on first boot only — afterwards the persisted toggle wins |
| `HELEN_ONLINE_MODE_PATH` | `data/online_mode.json` | Override the persisted toggle file location |

All adapters are **opt-in** — without the env var nothing changes.
All require LAN-internal targets (no public services). Failures
log a warning and continue with the default backend.

### Optional dependencies

Install only the groups you actually deploy:

```bash
pip install -r requirements.txt              # default
pip install -r requirements-extras.txt       # all optional backends
# Or single group:
pip install nats-py>=2.6.0                   # NATS only
pip install paho-mqtt>=2.0.0                 # MQTT only
pip install grpcio>=1.60.0 grpcio-tools      # gRPC only
```

WireGuard backend doesn't need a Python dep — it shells out to the
`wg` / `wg-quick` CLI which must be on PATH.

### Admin endpoints for new backends

```
GET /api/admin/transports/backends         summary of every backend
GET /api/admin/transports/nats/status      NATS adapter detail
GET /api/admin/transports/mqtt/status      MQTT adapter detail
GET /api/admin/transports/zeromq/status    ZeroMQ adapter detail
GET /api/admin/transports/rabbitmq/status  RabbitMQ adapter detail
GET /api/admin/transports/grpc/status      gRPC server detail
GET /api/admin/transports/wireguard/status WG mesh detail
GET /api/admin/transports/ssh/status       SSH tunnels detail
GET /api/admin/transports/turn/health      STUN binding + TURN allocate self-test
GET /api/admin/wan/portmap/status          UPnP + vendor instructions + peer probes
POST /api/admin/wan/portmap/refresh        Force a UPnP re-map
POST /api/admin/wan/probe-back             Peer-callable reachability probe
GET /api/admin/dns/stats                   Pi-hole-style DNS counters (queries/blocks/cache)
GET /api/admin/ops/stun/status             Self-hosted STUN responder stats
GET /api/admin/ops/federation/discovery    mDNS-discovered federation candidates
POST /api/admin/ops/federation/discovery/drain   Atomically pop the candidate list
GET /api/admin/ops/backup-verifier/status  Verifier history + last result
POST /api/admin/ops/backup-verifier/run-now      Trigger verification immediately
GET /api/admin/ops/federation/shaper/stats Per-peer bandwidth shaper stats
GET /api/online-mode/status                Public read — every authed user (for client UI)
POST /api/admin/online-mode/enable         Admin-only — flip the master toggle on
POST /api/admin/online-mode/disable        Admin-only — flip the master toggle off
GET /api/admin/online-mode/full-status     Admin-only — includes flip history + actor info
```

## Online-Mode master toggle

A single, persistent, **off-by-default** switch in
`app/services/online_mode_gate.py`. Every Helen feature that *can*
reach the public internet (WAN port-forward, DNS upstream forwarding,
etc.) registers itself with the gate at boot and stays dormant until
the operator deliberately flips it on. Buttons exposed:

* **Server** — `/api/admin/online-mode/{enable,disable}` REST endpoints.
* **Admin panel** — top card of the Overview tab in
  `CommClient-Server/admin/index.html` (loaded from
  `admin/online-mode.js`).
* **Desktop client** — `OnlineModePill` in the title bar
  (`CommClient-Desktop/src/renderer/components/online-mode/`).
  Read-only for non-admins, clickable for admins.

## Chat media bubbles (desktop)

Chat attachments are rendered by dedicated bubble components in
`CommClient-Desktop/src/renderer/components/chat/media/`:

| Sender uploads | Receiver sees | Action path |
|---|---|---|
| Image (`jpg/png/gif/webp/…`) | Inline thumbnail + click → Lightbox | existing `MessageBubble.tsx:347` |
| Voice/audio note | Inline player + waveform + speed | `VoiceMessageBubble` → `VoicePlayer` |
| Video (`mp4/mkv/mov/webm/avi/…`) | Tile with poster + ▶ → `VideoPlayerModal` (HTML5 `<video>` for Chromium-playable formats; "فتح خارجي" hands exotic codecs to the OS default app) | `VideoMessageBubble.tsx` |
| Anything else (PDF/Word/zip/exe/…) | Extension icon + size + "تحميل وفتح" / "تحميل فقط" / Reveal-in-folder | `FileMessageBubble.tsx` |

Downloads land in the user's Downloads folder via the Electron IPC
`downloads:stream-url` (with progress events) and open through
`shell.openPath` so the OS picks the right app. Browser-mode
fallback uses a vanilla `<a download>` anchor when
`window.electronAPI.downloads` isn't available.

## When in doubt

- The living manifest is `DELIVERY-MANIFEST.md`. Each session
  appends a section describing what was changed/added/rebuilt.
- The Arabic operator guide is `USER-GUIDE-AR.md`.
- Tests that exercise new wiring live in
  `CommClient-Server/tests/test_*_routes.py` and
  `test_*_endpoints.py`.
