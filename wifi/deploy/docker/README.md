# Helen — Docker Compose Stack

Production-grade stack for a Helen LAN deployment using Docker. Bundles
the server, optional rendezvous, nightly backup sidecar, and an optional
Prometheus scraper. Designed for **internal use only** — every port
binding is intended for RFC1918 networks.

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | The stack definition |
| `.env.example` | Template — copy to `.env` and fill in secrets |
| `prometheus.yml` | (optional) metrics scrape config |

## One-time setup

```bash
# 1. Load images (must be on the host already; download is offline)
docker load -i ../../helen-server-1.0.0.docker.tar

# 2. Configure secrets
cp .env.example .env
sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$(openssl rand -hex 32)|" .env

# 3. Start
docker compose up -d

# 4. Verify
docker compose ps
docker compose logs -f helen-server
curl http://localhost:3000/api/health
```

## Routine operations

```bash
# View logs
docker compose logs -f helen-server

# Stop / start / restart
docker compose stop helen-server
docker compose start helen-server
docker compose restart helen-server

# List backups (sidecar runs daily at 02:00)
docker compose exec helen-backup ls -lh /backups

# Manual backup right now
docker compose exec helen-backup /etc/periodic/daily/helen-backup

# Restore from a backup
docker compose stop helen-server
docker run --rm -v helen_helen-data:/data -v "$PWD":/in alpine \
  sh -c "cd /data && rm -rf * && tar xzf /in/helen-backup-XXX.tar.gz --strip-components=1"
docker compose start helen-server

# Update to a new image
docker load -i helen-server-1.0.1.docker.tar
sed -i 's|helen-server:1.0.0|helen-server:1.0.1|' docker-compose.yml
docker compose up -d helen-server
```

## Tear down (preserves data volumes)

```bash
docker compose down
```

## Tear down (destroys data — careful!)

```bash
docker compose down -v
```

## Multi-host deployment

Compose runs on a single host. For multi-host you have two options:

1. **Docker Swarm** — `docker stack deploy -c docker-compose.yml helen`
2. **Independent compose stacks** — one per host, with each running its
   own helen-server. Federation happens via the rendezvous (deployed once
   on a host visible to all subnets).

For a 5+ host LAN, prefer the **Ansible playbook** at
`../ansible/site.yml` over compose-per-host — it manages users, systemd,
firewall, certs, and rolling upgrades centrally.

## Security

- All containers run with `cap_drop: ALL` and `no-new-privileges: true`.
- The `helen-internal` network is a private bridge — nothing escapes
  it unless you publish a port via `ports:`.
- Volumes are local-only; backup volume can be bind-mounted to your NAS
  for off-host retention if needed.
- The Prometheus scraper sits inside the same internal network — it
  can't be reached from the LAN unless you uncomment its `ports:` line.
