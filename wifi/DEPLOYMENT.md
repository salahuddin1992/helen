# Helen / CommClient — Deployment Runbook

This is the operator-facing guide for installing Helen on a Windows
LAN, joining a federated mesh, and operating it day-to-day. It assumes
you have the pre-built installers (`Helen Desktop Setup 1.0.0.exe`,
`Helen-Server-Setup-1.0.0.exe`, `Helen-Admin-Setup-1.0.0.exe`,
`Helen-Rendezvous-Setup-1.0.0.exe`).

For development/build instructions see `PACKAGING.md`. For architectural
detail see `ARCHITECTURE-CONNECTION-AUDIT.md` and the `WORK_LOG.md`.

---

## 1. Component overview

| Component | Purpose | Process | Default port |
|---|---|---|---|
| Helen-Server.exe | Backend (FastAPI + Socket.IO + SQLite) | PyInstaller bundle | TCP 3000 (HTTP), 3443 (HTTPS sidecar), UDP 41234 (broadcast), UDP 5353 (mDNS) |
| Helen-Admin.exe | Operator console (PyWebView) | PyWebView host + child Helen-Server | TCP 5173 (admin HTML) |
| Helen Desktop.exe | End-user client (Electron + React) | Electron renderer | — |
| Helen-Rendezvous.exe | Public VPS reverse-tunnel proxy (optional) | FastAPI | TCP 9090, 9101, 9102 |
| Helen-Shell.exe | Multi-app launcher (optional) | Electron wrapper | — |
| sfu-worker (Node) | mediasoup SFU for >4-participant calls | child of Helen-Server | TCP 4443 (control) |

---

## 2. Single-machine LAN deployment (most common)

The simplest setup. One Windows box runs Helen-Server, every other box
on the same WiFi/LAN runs Helen Desktop. No internet required.

### Steps

1. **Install Helen-Admin** on the box that will host the server.
   Helen-Admin auto-spawns a child Helen-Server on first launch.
2. Run `Helen-Admin.exe`. Wait for tray icon "Helen-Admin Running".
3. Open the admin dashboard at `http://127.0.0.1:5173`.
4. Register the **first user** — by convention this user is auto-promoted
   to `role=admin` (see `auth_service.register` first-user bootstrap).
5. **Note the LAN IP** shown in the admin dashboard (e.g. `192.168.1.34`).
6. On every client machine, install **Helen Desktop**. Launch and let it
   discover the server via UDP broadcast (or paste `http://<LAN IP>:3000`
   into Advanced Settings).

### Verify

- `http://<LAN IP>:3000/api/health` returns `{"status":"ok"}`.
- `http://<LAN IP>:3000/api/discovery` returns the server profile.
- `http://<LAN IP>:5173/admin` shows the admin dashboard.
- Helen Desktop on each client lands on the chat list within ~3 seconds.

---

## 3. Cross-network access (Rendezvous tunnel)

When clients are on a different network than the server (cellular,
remote office, hotel WiFi, behind a corporate firewall), Helen can
relay through a public Rendezvous VPS.

### One-time VPS setup

1. Provision a Linux VPS with a public IP (any provider, ≥1 GB RAM).
2. Install `Helen-Rendezvous` (Linux build) or run `python main.py`
   directly. See `Helen-Rendezvous/README.md` for the full systemd
   unit example.
3. Set environment:
   - `HELEN_RENDEZVOUS_TOKEN=<32+ char random>` — required, fail-closes
     if missing.
   - `HELEN_RENDEZVOUS_PORT=9090` (default).
4. Start: `python main.py` (or via systemd).
5. Verify: `curl http://<VPS-IP>:9090/status` returns `{"tunnels":[]}`.

### Wire your Helen-Server to the tunnel

In Helen-Server's environment (set via `.env` or `Helen-Admin →
Connectivity` panel):

```
HELEN_RENDEZVOUS_WS_URL=ws://<VPS-IP>:9090/tunnel/register
HELEN_RENDEZVOUS_TOKEN=<same token>
HELEN_RENDEZVOUS_NAME=Helen-Prod-1
```

Restart Helen-Server. Verify in the admin dashboard:

- **Connectivity panel** shows `reverse_tunnel: active` with a
  `public_id` like `01b161949cf14`.
- The tunnel URL is now `http://<VPS-IP>:9090/t/<public_id>`.
- `curl http://<VPS-IP>:9090/t/<public_id>/api/health` returns
  `{"status":"ok"}` (proves the data plane works).

### Wire clients to the tunnel

On every Helen Desktop client → Settings → Advanced → Rendezvous URL:
paste the tunnel URL `http://<VPS-IP>:9090/t/<public_id>`.

The client now uses a `path: "/t/<public_id>/socket.io/"` connect option
so Socket.IO upgrades through the rendezvous WS proxy. Verify with the
splash screen "Connection diagnostics" — `transport=websocket` confirms
no polling fallback.

---

## 4. Federation (multi-server mesh)

Two or more Helen servers on the same cluster can share users so a
chat or call originated on Server A can reach a user logged into
Server B without bridging by hand.

### Configuration on every server

```
# Required.
FEDERATION_ENABLED=true
FEDERATION_SECRET=<32+ random bytes — same across the mesh>
COMMCLIENT_CLUSTER_ID=production-1   # all peers must match
HELEN_DISCOVERY_SECRET=<16+ chars — same across the mesh>

# Default acceptance: peers wait for an admin click in
# Admin → Peers before they can route fabric events.
COMMCLIENT_PEER_ACCEPTANCE_MODE=manual_approval
# auto_accept is acceptable for trusted-LAN testbeds only.

# Optional — seed peers when UDP broadcast is blocked.
HELEN_SEED_PEERS=http://server2.lan:3000,http://server3.lan:3000

# Optional — speed up presence propagation in a small mesh.
HELEN_FEDERATION_PRESENCE_RESYNC_SECONDS=15
```

### Approval flow

When a new server announces itself it lands in `WAITING_MANUAL_APPROVAL`
on every receiver. An admin opens **Admin → Peers → Pending** in the
Helen Desktop admin panel (or `iOS-Admin/web-simulator`) and clicks
**Approve**. The peer transitions to `READY` and becomes routable.

### Verify

The repo ships a live multi-server harness at
`CommClient-Server/tests/live/topology_harness.py`:

```bash
python tests/live/topology_harness.py A   # 1 server, 3 clients
python tests/live/topology_harness.py B   # 2 servers federated
python tests/live/topology_harness.py C   # 3-server chain
```

All three should print `PASS` after the most recent fixes.

---

## 5. SFU (mediasoup) for large group calls

Mesh group calls work up to `MESH_MAX_PARTICIPANTS=4` participants.
Beyond that, Helen-Server promotes to SFU automatically — but only
if the mediasoup-worker child process is running.

### Auto-launch (default)

When `Helen-Server.exe` starts and `COMMCLIENT_SFU_AUTOSTART_DISABLED`
is unset, the launcher (`app/services/sfu_launcher.py`):

1. Looks for `sfu-worker/` next to the server (or in `_MEIPASS/sfu-worker`
   for frozen builds).
2. Runs `npm install` if `node_modules/` is missing (skip with
   `COMMCLIENT_SFU_SKIP_INSTALL=1` for image-baked deployments).
3. Spawns `node src/server.js` with `MEDIASOUP_CONTROL_TOKEN` env so
   the parent and child share an HMAC.
4. Captures stdout/stderr to `data/sfu-worker-*.log` for postmortem.
5. Restarts on non-zero exit with exponential backoff.

**Pre-req:** Node.js 18.19+ on the host. `npm` must be on `PATH`.

### Externally-managed worker

If you run mediasoup outside Helen (e.g. dedicated container):

```
COMMCLIENT_SFU_EXTERNAL=1
MEDIASOUP_CONTROL_HOST=<sfu-host>
MEDIASOUP_CONTROL_PORT=4443
MEDIASOUP_CONTROL_TOKEN=<shared HMAC>
```

### Verify

- **Admin panel → Diagnostics** has a "SFU Status" card showing
  `Enabled / Running / Healthy / Restarts`.
- `GET /api/admin/sfu/status` returns `{"healthy": true, ...}`.
- Initiate a 5+ user video call; topology_manager will pick `sfu` mode.

---

## 6. TURN/STUN (NAT traversal)

For calls between users behind different NATs, Helen needs a TURN
server. Deployment kit at `CommClient-Server/deploy/coturn/`.

```bash
cd CommClient-Server/deploy/coturn
docker compose up -d
./health-check.sh
```

Wire Helen-Server:

```
TURN_SECRET=<long random>
TURN_REALM=commclient.local
TURN_HOST=<turn-host>
TURN_PORT=3478
```

Clients fetch credentials at `/api/turn/ice-config` and the desktop's
`iceConfigService.ts` caches them with TTL. Without TURN, calls fall
back to STUN-only (works on home/office NAT, fails on symmetric NAT).

---

## 7. Backups

Helen-Server's `backup_service` runs an hourly timer that snapshots
the SQLite DB + file uploads to `data/backups/`.

| Action | Where |
|---|---|
| List | Admin → Backups (or `GET /api/admin/backups`) |
| Run now | "إنشاء نسخة الآن" button (or `POST /api/admin/backups/run-now`) |
| Verify | Per-row "تحقق" button — replays the SQLite into a temp DB |
| Restore | Per-row "استعادة" — server stops + restores + restarts |
| Delete | Per-row "حذف" |
| Download | Per-row download button — direct file stream |

Off-host backups: rsync the `data/backups/` directory to a separate
machine on a cron schedule. Server writes integrity hashes alongside
each backup so you can verify on the secondary.

---

## 8. Monitoring (Prometheus)

Helen exposes Prometheus metrics at `/api/metrics`. Three auth modes:

| `HELEN_METRICS_TOKEN` | `HELEN_METRICS_PUBLIC` | `HELEN_ENV` | Behavior |
|---|---|---|---|
| set | — | — | Bearer-token gated (Prometheus scrape job) |
| unset | — | — | Admin role required (JWT) |
| unset | `1` | not `production` | Public read |
| unset | `1` | `production` | Refused |

### Prometheus scrape job

```yaml
scrape_configs:
  - job_name: helen
    authorization:
      credentials_file: /etc/prometheus/helen-token
    metrics_path: /api/metrics
    static_configs:
      - targets: ['helen-001:3000', 'helen-002:3000']
```

### Key metrics

- `helen_route_executor_events_total{outcome=...}` — fabric envelope flow.
- `helen_priority_queue_depth{priority=P0..P4}` — queue saturation.
- `helen_load_health_score` — 0..1 derived health (cpu/mem/lag).
- `helen_bcrypt_max_parallel` / `_in_flight` / `_waiting` — auth queue depth.
- `helen_active_sockets_total` / `helen_active_users_total`.
- `helen_peer_state_count{state=...}` — federation peer states.
- `helen_ack_events_total{outcome=acked|retried|...}`.

### Suggested alerts (Grafana/Alertmanager)

- `helen_load_event_loop_lag_ms > 200` for 1 min — server is wedged.
- `helen_bcrypt_waiting > 20` for 30 s — auth storm in progress.
- `helen_peer_state_count{state="REJECTED_BY_ADMIN"} > 0` — denied
  peer keeps trying; investigate.
- `up{job="helen"} == 0` — server down (standard).

---

## 9. Operator runbooks

For incidents see `RUNBOOKS.md`. Eight scenarios covered:

1. **RB-1** — server down (process exited)
2. **RB-2** — federation bridge down
3. **RB-3** — high ICE failure rate (TURN issues)
4. **RB-4** — DB unavailable
5. **RB-5** — TURN down
6. **RB-6** — call state inconsistency
7. **RB-7** — memory leak suspicion
8. **RB-8** — disconnect spike

Each runbook has diagnosis steps, likely causes, recovery, post-incident
verification.

---

## 10. Security checklist before exposing to a hostile network

- [ ] `JWT_SECRET` rotated to a 32+ byte random (default in `.env.example`
      is for dev only).
- [ ] `FEDERATION_SECRET` set if federation is enabled.
- [ ] `HELEN_DISCOVERY_SECRET` set if you want signed UDP broadcasts
      (mDNS spoofing protection).
- [ ] `MEDIASOUP_CONTROL_TOKEN` set (auto-launcher does this; verify
      env shows it).
- [ ] HTTPS sidecar enabled (default port 3443 with self-signed cert
      generated on first boot).
- [ ] `RATE_LIMIT_GLOBAL_ENABLED=true` (default).
- [ ] Reverse-proxy in front (nginx/caddy) terminating TLS, rate-limit,
      WAF — not strictly required on a LAN.
- [ ] Backups going off-host on a schedule.
- [ ] Admin user MFA — track via the existing session-revocation flow;
      true MFA is on the roadmap.
- [ ] `COMMCLIENT_PEER_ACCEPTANCE_MODE=manual_approval` (default; only
      `auto_accept` for trusted lab/testbed).
- [ ] Logs forwarded to your SIEM (Helen writes structured JSON via
      structlog — pipe stdout to your collector).

---

## 11. Performance / capacity

Verified on a 14-core / 64 GB Windows 11 host running Helen-Server.exe
unaccompanied (no other CPU-heavy work):

| Metric | Result |
|---|---|
| Concurrent registrations N=1000 | 1000/1000 succeed in 213 s |
| Concurrent socket connects N=1000 | 1000/1000 succeed in 12.2 s |
| Group join N=1000 (single channel) | 998/1000 (capped at MAX 500 for video) |
| Chat broadcast p80 N=1000 in-channel | 3174 ms (LAN) |
| Chat broadcast p80 N=64 in-channel | 82 ms (LAN) |
| File upload 256 KB | ~110 ms |

For larger-than-LAN deployments, run multiple Helen-Server instances
in a federation behind a Redis adapter (`HELEN_REDIS_URL=redis://…`).

---

## 12. Upgrades

Built-in updater in Helen Desktop polls the `electron-builder`
publish channel. To upgrade:

1. Replace `dist/Helen-Server/` with the new build.
2. Restart Helen-Server (or kill via Helen-Admin → Service strip).
3. Helen Desktop clients auto-update on next launch (or
   File → Check for Updates).

Backups are forward-compatible: restoring a v1.0.0 backup into v1.1.0
runs Alembic migrations on the imported DB before serving traffic.

---

## 13. Useful URLs

| Path | Purpose |
|---|---|
| `/api/health` | Liveness check (no auth) |
| `/api/discovery` | Server profile + cluster info (no auth) |
| `/api/connection/diagnostics` | Per-client connection report |
| `/api/admin/...` | Admin dashboard endpoints (admin role) |
| `/api/metrics` | Prometheus scrape (token or admin) |
| `/admin/` | Browser admin dashboard (Arabic RTL) |
| `/admin-mobile/` | iOS-Admin web simulator (PWA-able) |
| `/mobile/` | iOS client web simulator (PWA-able) |
| `/admin-secret/` | Master-code admin panel (emergency) |
