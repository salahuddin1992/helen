# Helen — Ansible Playbook

Roll out a complete Helen deployment (server + rendezvous + clients)
across an entire LAN with one command.

## Prerequisites on the controller

- Ansible 2.14+
- SSH access to every target (Windows targets need OpenSSH or WinRM)
- All build artefacts present in `{{ helen_artefacts_dir }}`:
  - `helen-server-linux-1.0.0.tar.gz`
  - `helen-rendezvous-linux-1.0.2.tar.gz` (if multi-LAN)
  - `commclient-desktop_1.0.0_amd64.deb` or `Helen Desktop-1.0.0.AppImage`
  - `Helen Desktop Setup 1.0.0.exe` (for Windows clients)

## Quick start

```bash
# 1. Stage artefacts
mkdir -p ~/helen-release
cp /path/to/helen-server-linux-1.0.0.tar.gz ~/helen-release/
cp /path/to/helen-rendezvous-linux-1.0.2.tar.gz ~/helen-release/
cp /path/to/commclient-desktop_1.0.0_amd64.deb ~/helen-release/

# 2. Configure inventory
cp inventory.example.ini inventory.ini
$EDITOR inventory.ini   # set hostnames + IPs

# 3. Generate the rendezvous token (multi-LAN only)
export HELEN_RENDEZVOUS_TOKEN=$(openssl rand -hex 32)

# 4. Run
ansible-playbook -i inventory.ini site.yml \
  -e "helen_rendezvous_token=$HELEN_RENDEZVOUS_TOKEN"

# 5. Verify
ansible -i inventory.ini helen_servers -m uri \
  -a "url=http://{{ ansible_default_ipv4.address }}:3000/api/health"
```

## What it does

| Group | Action |
|---|---|
| `helen_servers` | Push tarball, generate JWT_SECRET, install systemd unit, configure firewall (RFC1918 only), enable + start, wait for `/api/health` |
| `helen_rendezvous` | Push tarball, install + start, wire all servers to it via `.env` |
| `helen_clients_linux` | Detect deb vs AppImage, install package |
| `helen_clients_windows` | Push `.exe` installer, run silently |

## Targeted runs

```bash
# Only servers
ansible-playbook -i inventory.ini site.yml --limit helen_servers

# Only Linux clients
ansible-playbook -i inventory.ini site.yml --limit helen_clients_linux

# Single host
ansible-playbook -i inventory.ini site.yml --limit helen-srv-01
```

## Rolling upgrade

```bash
# Drop a new tarball
cp helen-server-linux-1.0.1.tar.gz ~/helen-release/

# Update var
ansible-playbook -i inventory.ini site.yml \
  --limit helen_servers \
  -e "helen_server_tarball=helen-server-linux-1.0.1.tar.gz" \
  --serial 1   # one host at a time, with health check between
```

## Idempotence

The playbook is fully idempotent — running it twice on the same hosts
makes no further changes once everything is in sync. It does NOT
overwrite `.env` files (so JWT_SECRETs are preserved across runs).
