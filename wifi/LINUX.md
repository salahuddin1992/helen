# Helen on Linux — Master Index

Production-grade three-component stack, packaged for Linux deployment.

| Component | Folder | Primary purpose | Install method | Operator CLI |
|---|---|---|---|---|
| **Helen-Server** | `Linux-Server/` | backend daemon (API, Socket.IO, SFU, DB) | systemd + hardening + backup timer | `helenctl` |
| **Helen-Admin** | `Linux-Admin/` | operator console (GUI + headless service) | systemd + launcher | `helen-admin-ctl` |
| **Helen Client** | `Linux-Client/` | end-user desktop app (Electron) | AppImage / deb / rpm | `helen-client-ctl` |

**Production features on top of plain systemd install:**
- Pre-flight & post-install verification (blocks on failure)
- AppArmor profile (complain mode; enforce on operator request)
- Kernel tunings (UDP buffers, somaxconn, swappiness) applied automatically
- Nightly backup timer (`/var/backups/helen/`, 14-day rotation, random jitter)
- Blue/green upgrade with auto-rollback (`helenctl upgrade`)
- Diagnostic bundler with secret redaction (`helenctl diag`)
- Prometheus textfile collector + Grafana dashboard shipped
- Kubernetes StatefulSet + HPA + PDB manifests in `Linux-Server/k8s/`
- Shell completion + manpage for `helenctl`
- TLS cert rotation helper (`helenctl cert rotate`)
- Cluster bootstrap commands (`helenctl cluster join/leave`)

Each folder has its own README, install script, and systemd/desktop
integration. Code shared across OSes lives in `CommClient-Server/` and
`CommClient-Desktop/` — the `Linux-*` folders wrap Linux-specific
packaging, paths, and service definitions around it.

---

## Quick start

On a single machine (server + admin + client side-by-side for testing):

```bash
# 1. build everything
./Linux-Server/scripts/build.sh
./Linux-Admin/scripts/build.sh
./Linux-Client/scripts/build-appimage.sh

# 2. install
sudo ./Linux-Server/install.sh
sudo ./Linux-Admin/install.sh
sudo ./Linux-Client/install.sh

# 3. use
sudo systemctl start helen-server           # server daemon
helen-admin                                  # admin GUI
helen                                        # client
```

Or separately on three machines:
- **Server host:** only `Linux-Server/install.sh`
- **Operator workstation:** `Linux-Admin/install.sh` (GUI)
- **End-user workstations:** `Linux-Client/install.sh`

---

## Paths after install (FHS-compliant)

| Purpose | Path |
|---|---|
| server binary | `/opt/helen/server/Helen-Server` |
| admin binary | `/opt/helen/admin/Helen-Admin` |
| client binary | `/opt/helen/client/Helen.AppImage` (AppImage mode) |
| server config | `/etc/helen/server.env` |
| admin config | `/etc/helen/admin.env` |
| server data | `/var/lib/helen/` |
| admin data | `/var/lib/helen-admin/` |
| server logs | `/var/log/helen/` |
| admin logs | `/var/log/helen-admin/` |
| client per-user data | `~/.config/Helen/` |
| systemd units | `/etc/systemd/system/helen-*.service` |
| CLI launchers | `/usr/bin/{helen-server, helen-admin, helen}` |
| desktop entries | `/usr/share/applications/helen{,-admin}.desktop` |

---

## Firewall ports (LAN exposure)

| Port | Proto | Purpose |
|---|---|---|
| 3000 | TCP | Helen-Server HTTP + Socket.IO |
| 3443 | TCP | Helen-Server HTTPS (self-signed) |
| 5173 | TCP | Helen-Admin static dashboard (loopback by default) |
| 41234 | UDP | LAN discovery broadcast |
| 5353 | UDP | mDNS (`helen.local`) |
| 40000–40100 | UDP | SFU media (tunable) |

Open only what the role on that host actually needs.

---

## systemd services shipped

| Service | Enabled by default? | Purpose |
|---|---|---|
| `helen-server.service` | yes (on `install.sh`) | server daemon |
| `helen-admin-headless.service` | no (opt-in via `--enable-service`) | admin dashboard over HTTP (no GUI) |

The GUI admin (`helen-admin`) runs as the operator — NOT a service.
Start it from the app menu or terminal like any desktop app.

---

## Building vs installing

`install.sh` in each folder EXPECTS the binary already built by the
matching `build.sh`. You can skip building if you're deploying to a
different machine and have copied the `dist/` artifact across.

Cross-distribution compatibility: build on the **oldest glibc** you
intend to support (Ubuntu 20.04 or CentOS 7). The resulting binary
runs on every newer distro. Musl-based distros (Alpine) need a
separate build.

---

## Docker deployment (server only)

```bash
cd Linux-Server
docker build -t helen-server .
docker run -d --name helen-server \
  -p 3000:3000 -p 3443:3443 \
  -p 41234:41234/udp -p 5353:5353/udp \
  -v helen-data:/var/lib/helen \
  -v helen-logs:/var/log/helen \
  helen-server
```

For admin + client, Docker is less useful (both benefit from the host
display/audio stack) — run them natively.

---

## Secret admin panel

Regardless of deployment mode, Helen-Server exposes a **separate**
admin surface at `http://<server>:3000/admin-secret/` gated by a
master code printed to the server console on first boot. It is not
part of Linux-Admin; open it in any browser. See
`CommClient-Server/admin-secret/`.

---

## Troubleshooting

**Server starts then exits:** check `sudo journalctl -u helen-server -n 100`
for the stacktrace. Most common: port 3000 already in use.

**Admin can't connect:** confirm `HELEN_ADMIN_BASE` in `/etc/helen/admin.env`
points at a reachable Helen-Server.

**Client doesn't auto-discover server:** firewall may block UDP 41234.
Override manually in Advanced Settings.

**SFU crashes on video join:** missing `libnspr` or `libnss3` on
minimal server distros. Install: `apt install libnspr4 libnss3`.

**Systemd unit fails with `Failed to create /run/helen`:** older
systemd versions don't support `RuntimeDirectory=`. Fall back by
removing that line and creating `/run/helen` in a preexec step.
