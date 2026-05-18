# Helen-Admin · Linux

Two deployment modes on Linux:

1. **GUI mode** — pywebview wraps the admin HTML in a native window
   (GTK/WebKit on GNOME, Qt on KDE). Runs as the logged-in user.
2. **Headless mode** — admin HTML served over HTTP only (no native
   window). Operator opens `http://localhost:5173` in any browser or
   reaches it over the LAN if `--expose-on-lan` is set. Runs as a
   systemd service under a dedicated user.

Choose GUI mode on an operator workstation; headless mode on a server.

## Filesystem layout (after `install.sh`)

```
/opt/helen/admin/                 binaries (pywebview + admin HTML)
/etc/helen/admin.env              environment config
/var/lib/helen-admin/             pywebview profile (cookies, localStorage)
/var/log/helen-admin/             admin logs
/usr/bin/helen-admin              launcher (GUI)
/etc/systemd/system/helen-admin.service   headless service
/usr/share/applications/helen-admin.desktop   app-menu entry
```

## Install

```bash
sudo ./install.sh
# default: installs binaries + .desktop + headless service (NOT enabled)
#
# --enable-service   also enable+start the headless systemd service
# --no-desktop       skip .desktop entry (for server boxes)
```

## Run modes

### GUI (interactive operator)
```bash
helen-admin
# opens native window; picks up /etc/helen/admin.env
```

Optional flags (passed through):
```bash
helen-admin --remote                  # don't auto-spawn a local Helen-Server
helen-admin --expose-on-lan           # also serve on 0.0.0.0:5173
helen-admin --no-autostart-server
```

### Headless (systemd service)
```bash
sudo systemctl enable --now helen-admin-headless
sudo systemctl status helen-admin-headless
# then visit http://<server-ip>:5173 from any browser
```

## Build

```bash
./scripts/build.sh
# produces  ../CommClient-Server/dist/Helen-Admin/Helen-Admin
```

Dependencies at build time: `python3-venv`, `python3-dev`, and a
webkit stack for pywebview:
```bash
# Debian/Ubuntu:
sudo apt install python3-gi gir1.2-webkit2-4.1 python3-gi-cairo \
                 libcairo2-dev libgirepository1.0-dev
# Fedora:
sudo dnf install python3-gobject webkit2gtk4.1 cairo-gobject-devel
```

## Environment variables (`/etc/helen/admin.env`)

| Variable | Default | Purpose |
|---|---|---|
| `HELEN_ADMIN_BASE` | http://localhost:3000 | initial server URL the dashboard targets |
| `HELEN_ADMIN_REMOTE` | 0 | 1 = never spawn a local Helen-Server |
| `HELEN_ADMIN_EXPOSE_ON_LAN` | 0 | 1 = bind 0.0.0.0 for the static admin HTTP |
| `HELEN_ADMIN_PORT` | 5173 | static-HTML HTTP port |

## Uninstall

```bash
sudo ./uninstall.sh
# optional --purge wipes /var/lib/helen-admin (pywebview profile)
```

## Secret admin panel

Alongside the main admin UI, the Helen-Server instance also exposes
`/admin-secret/` which is a separate realm gated by a master code
(printed to the server console on first boot). Access it in a regular
browser — not through Helen-Admin. See `CommClient-Server/admin-secret/`.
