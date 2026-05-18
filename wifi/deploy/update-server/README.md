# Helen Internal Update Server

A tiny static-file update server for distributing Helen releases inside
your LAN, with no internet dependency. Helen-Server / Helen-Desktop /
Helen-Mobile clients can be configured to poll this URL on startup.

## Why

Without this, every host has to be manually updated. With this:

- One person publishes a new release tarball/exe to `/var/lib/helen-updates/`
- All clients see "Update available" the next time they restart
- Updates flow over the LAN — no Microsoft, no Apple, no Google involved

## Layout

```
/var/lib/helen-updates/
├── manifest.json              # Single source of truth — versions + hashes
├── server/
│   ├── 1.0.0/
│   │   ├── helen-server-linux.tar.gz
│   │   ├── helen-server-linux.tar.gz.sha256
│   │   ├── Helen-Server-Setup-Windows.exe
│   │   └── Helen-Server-Setup-Windows.exe.sha256
│   └── 1.0.1/   (when published)
├── rendezvous/
│   └── 1.0.2/...
├── desktop/
│   ├── 1.0.0/...
│   └── stable/  (symlink to current)
└── mobile/
    └── 1.0.0/...
```

The server (Helen-Server.exe) reads `manifest.json` and exposes its
contents via `/api/updates/check?platform=linux&channel=stable&current=1.0.0`.
The client UI surfaces the result.

## Deploy

### Option 1 — nginx (recommended)
```bash
sudo apt install nginx
sudo cp helen-updates.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/helen-updates.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### Option 2 — Caddy (simplest, auto-HTTPS for internal CA)
```bash
sudo apt install caddy
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

### Option 3 — Just `python -m http.server` (testing only)
```bash
cd /var/lib/helen-updates
python3 -m http.server 8888
```

## Adding a release

```bash
# Drop a new release into the right slot
sudo mkdir -p /var/lib/helen-updates/server/1.0.1
sudo cp helen-server-linux-1.0.1.tar.gz /var/lib/helen-updates/server/1.0.1/
( cd /var/lib/helen-updates/server/1.0.1 && \
  sha256sum *.tar.gz > helen-server-linux.tar.gz.sha256 )

# Update manifest
sudo bash gen-manifest.sh > /var/lib/helen-updates/manifest.json

# Optional: set the "stable" symlink
sudo ln -sfn 1.0.1 /var/lib/helen-updates/server/stable
```

## Client configuration

Helen clients read from `HELEN_UPDATE_URL` env var. Set it once when
provisioning:

```ini
# /opt/helen-server/.env
HELEN_UPDATE_URL=http://updates.lan.local/manifest.json
HELEN_UPDATE_CHANNEL=stable
```

## Security

- The manifest is signed (planned — see `gen-manifest.sh`) so a rogue
  update server can't push a fake release.
- Tarballs include SHA-256 checksums; clients verify before installing.
- The update host should be on a restricted segment so only HTTPS GETs
  are accepted from clients.
- Running over HTTPS with the same self-signed cert your other Helen
  components use is recommended (see `tools/self-sign-helen.ps1`).
