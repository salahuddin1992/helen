# Helen — System Architecture

Last updated: 2026-05-06

This document is the single source of truth for how Helen's
components fit together at runtime. Pair it with `CLAUDE.md`
(operator quick-reference) and `DELIVERY-MANIFEST.md` (release
notes per session).

---

## 1. Components

### Servers (8 types)

| # | Component | Role | Default port(s) |
|---|---|---|---|
| 1 | **Helen-Server** | FastAPI + Socket.IO backend; owns chat, calls, files, channels | 3000 (HTTP) / 3443 (HTTPS) |
| 2 | **Helen-Router** | Mandatory entry point + reverse proxy + service registry + mesh | 8080 |
| 3 | **Helen-Rendezvous** | NAT traversal between LAN subnets | 9090 + relay 9101/9102 |
| 4 | **Helen-Admin** (`CommClient-Admin`) | PyWebView-based admin UI | 5173 (loopback only) |
| 5 | **mediasoup SFU worker** | RTP forwarding for calls > 6 participants | 4443 control / 40000-49999 RTP |
| 6 | **coturn (bundled TURN)** | NAT-relay for WebRTC peers behind hard NAT | 3478 / 5349 |
| 7 | **Internal DNS** (Helen-Router) | Resolves `*.helen.lan` | 53 |
| 8 | **Internal NTP** (Helen-Router) | SNTPv4 (RFC 5905) for clock sync | 123 |

### Clients

| Platform | Build chain | Output |
|---|---|---|
| Desktop (Windows) | electron-builder NSIS | `Helen Desktop Setup 1.0.0.exe` (~115 MB) |
| Desktop (Linux) | electron-builder | `.deb`, `.AppImage`, `.tar.gz` |
| Mobile (Android) | Capacitor + Gradle | `*.apk` debug+release, `*.aab` |
| iOS | GitHub Actions on Mac | Native Swift app |
| Web PWA | Vite build | Static bundle served by Helen-Server `/desktop/` |

---

## 2. Topology

```
                ┌────────────────┐
                │ Helen Desktop  │ ─┐
                │ Helen Mobile   │  │   (any LAN client)
                │ Web PWA        │  │
                └────────────────┘  │
                                    │  TLS
                                    ▼
        ┌─────────────────────────────────────────┐
        │          Helen-Router (8080)            │  ← mandatory entry
        │  reverse proxy + service registry +     │     when HELEN_REQUIRE_ROUTER=1
        │  mesh (Dijkstra/Ring) + DNS + NTP       │
        └────────┬────────────────┬───────────────┘
                 │                │
       RTT-ranked│                │ failover chain
        ▼        ▼                ▼
  ┌──────────┐ ┌──────────┐  ┌──────────┐
  │ Helen-   │ │ Helen-   │  │ Helen-   │  ← multi-server federation
  │ Server A │ │ Server B │  │ Server C │     (HMAC-JSON or gRPC)
  └────┬─────┘ └────┬─────┘  └────┬─────┘
       │            │             │
       │ broker fan-out (Redis Streams / NATS / MQTT)
       ▼            ▼             ▼
  ┌──────────────────────────────────┐
  │   mediasoup SFU (calls)          │
  │   coturn TURN (NAT relay)        │
  │   Helen-Rendezvous (cross-subnet)│
  └──────────────────────────────────┘
```

Optional **WireGuard mesh** can sit between Helen-Servers when
they live across untrusted links — every byte is then encrypted
end-to-end on top of TLS.

---

## 3. Data flow — three canonical paths

### 3.1 DM message (1 ↔ 1)

```
Alice → Router → Server-A → DB (messages)
                          → Socket.IO emit_to_user(Bob)
                          ↓
                       Bob's socket
```

Cross-server case: `fabric_emit` publishes to broker (Redis/NATS/MQTT)
keyed by `destination_user_id`. Server-B's broker subscriber picks up
the envelope, runs `route_executor._exec_local_deliver()`, emits to
Bob's local Socket.IO sids.

### 3.2 Group call (e.g. 50 participants)

1. First joiner: `v2_call_join_group` → `call_service.initiate_call(routing="mesh")`
2. Each subsequent join feeds:
   - `sfu_orchestrator.observe_participant_count(call_id, n)`
   - `large_call_orchestrator.on_join(call_id, user_id, role)`
3. At 7+ participants, the **SFU orchestrator** broadcasts
   `call:topology_change` with `{topology: "sfu"}` → clients
   tear down their mesh PeerConnections and re-attach to mediasoup
   transport.
4. The **large-call orchestrator** picks a tier from
   `topology_for_count`:
   - `mesh` (3-6) → P2P PeerConnections
   - `sfu_small` (7-50) → 1 SFU worker, full forwarding
   - `sfu_large` (51-200) → SFU + last-N video budget
   - `sfu_xlarge` (201-500) → SFU + simulcast layers
   - `webinar` (501-2000) → presenter + audience role split
   - `federated_webinar` (2001+) → multiple SFU workers across servers

### 3.3 Calendar reminder

1. `ReminderWorker` polls `calendar.db` every 60s (started in lifespan).
2. When an event's `start_at - reminder_minutes` matches now ± 30s,
   the worker calls `emit_to_user(user_id, "calendar:reminder", payload)`.
3. Socket.IO server checks the user's online sids and emits.
4. If user is offline, `lan_push_manager.push()` queues the
   notification for up to 24h and (optionally) sends a Wake-on-LAN
   magic packet to wake a sleeping desktop.

---

## 4. Optional backend matrix

Helen ships with sensible defaults but every layer can be swapped:

| Layer | Default | Alternates | Selector env var |
|---|---|---|---|
| Pub/sub broker | Redis Streams | NATS, MQTT, in-memory | `HELEN_BROKER_BACKEND` |
| Inter-server RPC | HMAC-JSON / HTTP/2 | gRPC | `HELEN_FEDERATION_BACKEND` |
| Mesh routing | Dijkstra full-mesh | Ring, hierarchical | `HELEN_MESH_TOPOLOGY` |
| VPN overlay | none | WireGuard | `HELEN_VPN_BACKEND` |
| DB | SQLite | PostgreSQL | `HELEN_DB_BACKEND` |
| DB at-rest crypto | none | SQLCipher | `HELEN_DB_ENCRYPTED` |

All alternates are **opt-in** and lazy-loaded — the optional
dependency is only imported when the env var is set.

---

## 5. Security model

### Defense layers, outermost first

1. **LAN-only middleware** (Helen-Router) — RFC1918 source-IP allowlist.
2. **Router token** — every proxied request stamped with
   `X-Forwarded-By: helen-router/<HELEN_ROUTER_TOKEN>`. Server with
   `HELEN_REQUIRE_ROUTER=1` rejects anything else with 403.
3. **JWT auth** — every API call needs `Authorization: Bearer …`,
   secret loaded from `.env` (mode 0600 + ICACLS lockdown on Windows).
   `_WEAK_JWT_SECRETS` set blocks known-leaked installer placeholders.
4. **RBAC** — capability flags on `User.role`; admin-only endpoints
   gated by `Depends(require_role("admin"))`.
5. **HMAC federation** — inter-server traffic carries
   `Authorization: HMAC-SHA256 <signature>` with a shared
   `HELEN_FEDERATION_SECRET`.
6. **TLS** — Helen-CA mints internal X.509 certs; clients use
   `https://10.0.0.x:3443` for browser camera/mic.
7. **At-rest encryption** (opt-in) — SQLCipher key from
   `data/db-master.key` (Argon2id-derived).
8. **E2EE** for sensitive messages — Signal-style X3DH + Double
   Ratchet via `app/services/e2ee_service.py`.
9. **Audit chain** — every `audit_log()` call mirrored to a
   tamper-evident Merkle hash chain. `verify()` runs every 5 min
   in lifespan.
10. **Crash reporter** — local SQLite store; never leaves the LAN.

---

## 6. Per-backend deployment recipes

### 6.1 Default (Redis Streams + HTTP federation)

```bash
# Helen-Server side
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env
echo "HELEN_REDIS_URL=redis://10.0.0.10:6379" >> .env
./Helen-Server.exe
```

No additional pip install needed. Works out of the box.

### 6.2 NATS broker

```bash
pip install nats-py>=2.6.0
export HELEN_BROKER_BACKEND=nats
export HELEN_NATS_URL=nats://10.0.0.10:4222
./Helen-Server.exe
# Verify:
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:3000/api/admin/transports/nats/status
```

### 6.3 MQTT broker

```bash
pip install paho-mqtt>=2.0.0
export HELEN_BROKER_BACKEND=mqtt
export HELEN_MQTT_HOST=10.0.0.5
export HELEN_MQTT_PORT=1883
export HELEN_MQTT_USERNAME=helen
export HELEN_MQTT_PASSWORD=...   # from internal credential store
./Helen-Server.exe
```

Topic pattern auto-translates: Helen subjects `fabric.P0.x.y` →
MQTT topics `helen/fabric/P0/x/y`.

### 6.4 gRPC federation

```bash
pip install grpcio>=1.60.0 grpcio-tools>=1.60.0 protobuf>=4.25.0
export HELEN_FEDERATION_BACKEND=grpc
export HELEN_GRPC_FEDERATION_PORT=50051
./Helen-Server.exe
```

The `.proto` schema compiles dynamically on first boot — no
`protoc` needed at deploy time.

### 6.5 WireGuard overlay

Linux:
```bash
sudo apt install wireguard-tools
export HELEN_VPN_BACKEND=wireguard
export HELEN_WG_LISTEN_PORT=51820
export HELEN_WG_MESH_SUBNET=10.99.0.0/24
# systemd unit needs:
#   AmbientCapabilities=CAP_NET_ADMIN
sudo -E ./Helen-Server
```

Windows: install official WireGuard MSI then set the same env vars
and run as Administrator.

### 6.6 Ring topology (Helen-Router)

```bash
# On every Helen-Router node:
export HELEN_MESH_TOPOLOGY=ring
./Helen-Router.exe
# Verify:
curl http://localhost:8080/router/topology-strategy
# → {"strategy":"ring", ...}
```

---

## 7. Observability

| What | Where |
|---|---|
| Server logs | structlog JSON to stdout; capture via journalctl on Linux, NSSM AppStdout on Windows |
| Crash reports | SQLite `data/crashes.db` — view via `/api/admin/crashes` |
| Audit chain | SQLite `data/audit_chain.db` — verify via `POST /api/admin/audit-chain/verify` |
| Metrics | Prometheus exposition at `/api/metrics` (admin or `HELEN_METRICS_TOKEN`) |
| Live calls | `/api/admin/active-calls`, `/api/admin/sfu-status` |
| Backend status | `/api/admin/transports/{nats,mqtt,grpc,wireguard}/status` |
| Mesh topology | `/mesh/topology` (Helen-Router), `/router/topology-strategy` |
| Health probes | `/api/health` (Server), `/router/health` (Router), `/health` (Rendezvous) |
| Deployment audit | `python tools/verify-deployment.py` — 9 checks, JSON report |

---

## 8. File layout

```
C:/Users/youse/c/wifi/
├── CommClient-Server/         Helen-Server source + venv
│   ├── app/                   FastAPI + Socket.IO + services
│   ├── tests/                 770+ pytest tests
│   ├── tools/                 verify-deployment.py, helen-ca.py, etc.
│   ├── scripts/               build-linux-wsl.sh, smoke-linux.sh
│   ├── deploy/linux/          systemd units, install scripts
│   ├── CommClient-Server.spec PyInstaller config
│   └── installer-server-only.nsi  NSIS installer
├── Helen-Router/              Mandatory entry + mesh
├── Helen-Rendezvous/          NAT traversal
├── CommClient-Desktop/        Electron renderer (React + Vite)
├── CommClient-Mobile/         Capacitor wrapper around the same renderer
├── iOS/                       Swift sources (Mac-built)
├── Vault/web/                 Static panel mounted by Server
├── docs/                      this file + USER-GUIDE-AR.md
├── deploy/                    docker, ansible, update-server
├── scripts/build-all.sh       One-shot rebuild of every artifact
├── DELIVERY-MANIFEST.md       Living changelog
└── CLAUDE.md                  AI assistant operator brief
```

---

## 9. Test inventory

| Suite | Count | Focus |
|---|---|---|
| Helen-Server `tests/` | 770+ | Routes, services, federation, calls |
| Helen-Router `test_*.py` | 14 (mesh) + 4 (failover/federation) | Routing logic, multi-node |
| Stress tests (Helen-Router) | 5 | 100 → 1M router scenarios |
| Integration tests (new transports) | 9 | NATS/MQTT/gRPC/WG/L2-L3/Ring |
| **Total session-added tests** | **46** | All passing in < 30s |

Run all:

```bash
cd CommClient-Server && JWT_SECRET=$(python -c 'import secrets;print(secrets.token_hex(32))') \
  python -m pytest tests/
cd ../Helen-Router && python -m pytest test_mesh_endpoints.py
```

---

## 10. Versioning

All components are pinned to `1.0.0` in their respective build
configs (PyInstaller specs, package.json, Capacitor build.gradle,
NSIS scripts). Bumping the version is a coordinated change across:

- `CommClient-Server/CommClient-Server.spec` (`version=`)
- `Helen-Router/Helen-Router.spec`
- `*/installer*.nsi` (`!define APP_VERSION`)
- `CommClient-Desktop/package.json` (`version`)
- `CommClient-Desktop/electron-builder.yml`
- `CommClient-Mobile/package.json`
- `CommClient-Mobile/android/app/build.gradle` (`versionName` + `versionCode`)
- `Helen-Rendezvous/main.py` (`version=`)

The `scripts/build-all.sh` orchestrator rebuilds everything in the
correct order; running it after a version bump produces a coherent
release set.
