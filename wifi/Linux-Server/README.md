# Helen-Server · Linux (production-grade)

Full production deployment stack for Helen-Server on Linux — systemd,
hardening, monitoring, backup, Kubernetes, operator CLI.

---

## Layout

```
Linux-Server/
├── install.sh                runs preflight, lays down binaries, enables service
├── uninstall.sh              stops, removes (preserves data by default)
├── bin/
│   └── helenctl              unified operator CLI (see "Operator toolkit" below)
├── scripts/
│   ├── preflight.sh          pre-install system checks (deps, ports, tunables)
│   ├── build.sh              PyInstaller bundle
│   ├── run-dev.sh            run from source (no install)
│   ├── healthcheck.sh        0/non-zero probe (for monitoring)
│   ├── diag-bundle.sh        collect redacted support tarball
│   ├── backup.sh             atomic DB + state snapshot (SQLite .backup)
│   └── restore.sh            restore with auto-rollback savepoint
├── systemd/
│   ├── helen-server.service         hardened unit (ProtectSystem, syscall filter)
│   ├── helen-server-backup.service  oneshot — calls backup.sh
│   └── helen-server-backup.timer    nightly @ 03:15 ±15m random jitter
├── config/
│   ├── helen-server.env      operator-editable env
│   ├── logrotate.conf        weekly, keep 8, compress
│   └── sysctl-helen.conf     UDP buffers, somaxconn, swappiness
├── apparmor/
│   └── usr.local.helen-server  AppArmor profile (complain by default)
├── completion/
│   └── helenctl.bash         bash tab-completion for helenctl
├── man/
│   └── helenctl.1            man page
├── k8s/
│   └── helen-server.yaml     StatefulSet + Service + PDB + HPA
├── monitoring/
│   ├── prometheus-scrape.yml example scrape + alert rules
│   ├── textfile-collector.sh JSON-to-prom bridge for node_exporter
│   └── grafana-dashboard.json ready-made dashboard
└── Dockerfile                two-stage container image
```

---

## Filesystem layout (after install)

FHS-compliant, fully enumerable:

```
/opt/helen/server/                   binaries + bundled Python runtime
/opt/helen/server/scripts/           helper scripts (diag, backup, restore)
/etc/helen/server.env                environment config (operator-edited)
/etc/sysctl.d/99-helen.conf          kernel tuning
/etc/apparmor.d/opt.helen.server.*   AppArmor profile
/etc/systemd/system/helen-server.*   units (service, backup service, backup timer)
/etc/logrotate.d/helen-server        log rotation policy
/etc/bash_completion.d/helenctl      tab-completion
/usr/bin/helen-server                launcher shim
/usr/bin/helenctl                    operator CLI
/usr/share/man/man1/helenctl.1       manpage
/var/lib/helen/                      persistent data (DB, roles, codes, audit)
/var/log/helen/                      server logs
/var/backups/helen/                  nightly snapshots (keep 14)
/run/helen/                          runtime sockets
```

---

## Install

```bash
sudo ./install.sh
# runs preflight, creates 'helen' user, copies binary, writes config,
# installs systemd units, enables the service + nightly backup timer,
# installs helenctl + man page + completion, applies sysctl tunables,
# loads AppArmor profile (in complain mode)
```

Verify:
```bash
helenctl status        # service state + live control-plane metrics
helenctl health        # 0 on green
helenctl check         # pre-flight + config validation
systemctl list-timers helen-server-backup
```

---

## Operator toolkit (`helenctl`)

One command for everything routine:

```
helenctl status              → service + phase + CPU/RSS/sockets/rooms
helenctl logs -f             → journalctl tail
helenctl health              → probe /api/health, exit 0/1
helenctl check               → preflight + config validator
helenctl diag [path]         → tar.gz bundle (redacted secrets)
helenctl backup [path] [--include-secret --include-config]
helenctl restore <bundle>    → auto-rollback on failure
helenctl upgrade <bin-dir>   → blue/green swap + smoke test
helenctl cert rotate         → regen self-signed w/ current LAN IP
helenctl cluster list        → registered nodes + strength/load
helenctl cluster join <url>  → register peer
helenctl role sfu off        → toggle a server role
helenctl policy audio_only   → force media mode
helenctl emergency freeze    → force frozen phase (needs secret token)
helenctl emergency exit      → leave emergency/frozen
helenctl roles               → print full role matrix
helenctl version             → binary info
man helenctl                 → full manpage
```

Tab-completion for all subcommands included.

---

## Hardening

Enabled automatically:

- **systemd sandbox:** `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`,
  `ProtectHome`, `ProtectKernel*`, `RestrictSUIDSGID`, syscall filter `@system-service`
  minus `@privileged`
- **Dedicated system user** (`helen`), no shell, no home login
- **Read-only FS** except explicit `ReadWritePaths=/var/lib/helen /var/log/helen /run/helen`
- **Port-binding capability** retained only if you lower PORT below 1024

Optional (operator toggles on):

- **AppArmor** profile shipped in complain mode. After validating fit:
  `sudo aa-enforce /opt/helen/server/Helen-Server`
- **SELinux**: an equivalent policy isn't shipped; most distros accept
  the service under `unconfined_service_t`. Custom policy is left to
  the operator since it depends heavily on policy baseline.
- **Firewall**: `firewall-cmd`/`ufw` commands in the main README; not
  auto-applied to avoid surprising the operator.

---

## Kernel tuning (`sysctl-helen.conf`)

Installed to `/etc/sysctl.d/99-helen.conf` and applied immediately.

Raises what matters on a media server:

| Tunable | Default | Helen value | Why |
|---|---|---|---|
| `net.core.rmem_max` | 212992 | 4194304 | UDP RX burst resilience |
| `net.core.wmem_max` | 212992 | 4194304 | UDP TX burst |
| `net.core.netdev_max_backlog` | 1000 | 5000 | short bursts of concurrent connects |
| `net.core.somaxconn` | 128 | 8192 | matches uvicorn's backlog arg |
| `net.ipv4.tcp_max_syn_backlog` | 128 | 8192 | same |
| `net.ipv4.tcp_keepalive_time` | 7200 | 60 | detect dead clients fast |
| `net.ipv4.tcp_tw_reuse` | 2 | 1 | reuse TIME_WAIT under churn |
| `vm.swappiness` | 60 | 10 | media servers must not swap |
| `fs.file-max` | ~8k | 262144 | plenty of headroom |

Revert by removing the file and running `sysctl --system`.

---

## Monitoring

### Prometheus + Grafana

- `monitoring/textfile-collector.sh` — run from cron/timer; writes
  `helen.prom` into node_exporter's textfile directory. Produces:
  - `helen_up` (reachability)
  - `helen_control_plane_phase{phase=normal|degraded|emergency|frozen}`
  - `helen_cpu_p95`, `helen_rss_p95`
  - `helen_active_sockets`, `helen_rooms_total`, `helen_admission_open`
- `monitoring/prometheus-scrape.yml` — drop-in scrape config + alert
  rules for Alertmanager
- `monitoring/grafana-dashboard.json` — import via Grafana UI; phase
  stat tile, sockets/rooms, CPU/RSS timeseries

### Plain systemd

Without Prometheus, `journalctl -u helen-server` + `helenctl status`
covers basic operations. Send alerts via a thin script hooked into
OnFailure= in the unit file if needed.

---

## Backup / restore

Daily snapshots automatically (systemd timer, 03:15 ±15m jitter):

```
/var/backups/helen/
  helen-2026-04-22_031537.tar.gz
  helen-2026-04-23_031642.tar.gz
  …kept 14 days…
```

Each snapshot is an atomic SQLite `.backup` + state files. Restore with:

```bash
sudo helenctl restore /var/backups/helen/helen-2026-04-22_031537.tar.gz
# service stops, old DATADIR moved to a savepoint, snapshot extracted,
# service restarts, auto-rollback on smoke-test failure
```

Ad-hoc backup:
```bash
sudo helenctl backup                       # without secrets or config
sudo helenctl backup --include-config      # plus /etc/helen/server.env
sudo helenctl backup --include-secret      # plus master code (use with care)
```

---

## Kubernetes

`k8s/helen-server.yaml` — StatefulSet (stable per-replica PVC), two
services (headless for pod DNS + regular with ClientIP session
affinity), PodDisruptionBudget, HorizontalPodAutoscaler (scale on CPU
>70% avg).

```bash
kubectl apply -f k8s/helen-server.yaml
kubectl -n helen get pods,svc,pdb,hpa
```

Scale-out:
```bash
kubectl -n helen scale statefulset/helen-server --replicas=3
```

The control plane's node registry + consistent-hash placer absorbs new
pods automatically — rooms rebalance on next placement decision.

---

## Cluster bootstrap (bare-metal multi-node)

```bash
# On node-1 (first box)
sudo ./install.sh
# Copy the node_id so node-2 can register against node-1
cat /var/lib/helen/node_id.txt

# On node-2
sudo ./install.sh
sudo HELEN_ADMIN_TOKEN=<admin-jwt> \
     helenctl cluster join http://node-1.local:3000
```

Each node continues hosting its local rooms. Placement for new rooms
will prefer the **strongest healthy node** automatically (strength =
CPU cores × 0.4 + RAM × 0.3 + NIC × 0.2 + SSD × 0.1).

---

## Diagnostic bundle

When support asks for data:
```bash
sudo helenctl diag /tmp
# → /tmp/helen-diag-2026-04-23-144501.tar.gz
```

Contains (with sensitive values redacted):
- `uname`, `/etc/os-release`, CPU/MEM/DISK
- Full systemd status + show
- journald (last 10k lines for server, 2k for admin)
- `/var/log/helen/` app logs
- `/etc/helen/server.env` with `SECRET|TOKEN|PASSWORD|KEY` fields redacted
- DB **schema + row counts** (never contents)
- Network: `ip addr`, `ss -lntu`, firewall rules
- Live `/api/discovery` output
- Access codes file with values truncated to first 4 chars

Never included: master code, password hashes, message content.

---

## TLS certificate lifecycle

Self-signed by default (LAN-only, no public CA):

```bash
sudo helenctl cert rotate
# regenerates /var/lib/helen/tls/helen.{crt,key}
# SANs: helen.local, localhost, 127.0.0.1, auto-detected LAN IP
sudo systemctl restart helen-server
```

For public-facing deployments behind a reverse proxy, terminate TLS at
the proxy and leave `HELEN_HTTPS_DISABLED=1` in server.env.

---

## Upgrade workflow

Blue/green via `helenctl upgrade`:

```bash
# 1. Build or receive new binary
./scripts/build.sh
# output: ../CommClient-Server/dist/Helen-Server/

# 2. Backup first
sudo helenctl backup --include-config

# 3. Upgrade (in-place swap with auto-rollback)
sudo helenctl upgrade ../CommClient-Server/dist/Helen-Server/
# → renames current /opt/helen/server to /opt/helen/server.prev.<ts>
# → copies new binary, restarts, smoke-tests /api/health
# → if health fails, rolls back automatically
```

If the old version is kept in `/opt/helen/server.prev.*`, delete when
you're confident the new one is stable:
```bash
sudo rm -rf /opt/helen/server.prev.*
```

---

## Unit file at a glance

Key clauses in `helen-server.service`:

```
ExecStart=/opt/helen/server/Helen-Server
User=helen  Group=helen
Restart=always  RestartSec=3  StartLimitBurst=10
LimitNOFILE=65536  TasksMax=8192
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/helen /var/log/helen /run/helen
SystemCallFilter=@system-service ~@privileged
```

---

## Operational recipes

**Watch a flapping service:**
```bash
sudo journalctl -u helen-server -f --since "5 min ago"
```

**See why a decision was suppressed:**
```bash
sudo tail -f /var/lib/helen/control_plane_audit.ndjson | jq '. | select(.suppressed)'
```

**Force audio-only cluster-wide for 10 minutes:**
```bash
HELEN_ADMIN_TOKEN=... helenctl policy audio_only
# wait 10 min
HELEN_ADMIN_TOKEN=... helenctl policy auto
```

**Remove a misbehaving peer node from the registry:**
```bash
helenctl cluster list
HELEN_ADMIN_TOKEN=... helenctl cluster leave <node-id>
```

**Emergency drain (planned maintenance):**
```bash
HELEN_SECRET_TOKEN=... helenctl emergency freeze    # refuse new rooms
# wait for active rooms to finish
sudo systemctl stop helen-server
# …maintenance work…
sudo systemctl start helen-server
HELEN_ADMIN_TOKEN=... helenctl emergency exit
```

---

## Sizing guidance

| Users × rooms | Disk | RAM | Cores | NIC |
|---|---|---|---|---|
| ≤50 × ≤3 | 50 GB SSD | 4 GB | 2 | 1 Gbps |
| 50–500 × 10–30 | 200 GB SSD | 16 GB | 4–8 | 1 Gbps |
| 500–5000 × 100+ | 1 TB NVMe | 32 GB × N boxes | 16 × N | 10 Gbps |

See `../LINUX.md` and `CommClient-Server/` for architecture details.
