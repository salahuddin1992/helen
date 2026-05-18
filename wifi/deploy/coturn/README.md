# Helen TURN — Production Deployment

This directory deploys a **coturn** server that pairs with Helen-Server's
`/api/turn/ice-config` endpoint. Clients fetch short-lived HMAC
credentials from Helen-Server and use them with this coturn to traverse
symmetric NAT.

Without this deployment, Helen falls back to LAN-only — symmetric-NAT
users (corporate, mobile carrier CGN) can't connect cross-network.

## Quick start

1. Install Docker + docker compose on the TURN host (any public-IP VPS).
2. Open firewall:
   ```
   ufw allow 3478/udp
   ufw allow 3478/tcp
   ufw allow 5349/tcp
   ufw allow 49152:65535/udp
   ```
3. Generate a strong shared secret:
   ```
   openssl rand -base64 48
   ```
4. Edit `turnserver.conf` — replace `static-auth-secret=...` with the value above.
5. Set the same value in Helen-Server's environment:
   ```
   export HELEN_TURN_SECRET="<paste-here>"
   export HELEN_TURN_REALM="commclient.local"   # must match
   systemctl restart helen-server
   ```
6. Bring up:
   ```
   cd deploy/coturn
   docker compose up -d
   docker compose logs -f
   ```
7. Validate:
   ```
   HELEN_TURN_SECRET=<secret> ./health-check.sh
   ```

## Verify from a Helen client

1. Open Helen Desktop, sign in.
2. Settings → Diagnostics → Run now.
3. The "Server" check should be green.
4. Start a video call to a peer in a different network.
5. Open `chrome://webrtc-internals` → Stats → look for a candidate-pair
   with `relay` type. If found, TURN is working.

## Monitoring

`health-check.sh` returns:
- `0` — TURN healthy
- `1` — port unreachable
- `2` — port up but allocate/relay failed

Hook into Prometheus blackbox or systemd OnFailure. Recommended cron:
```
* * * * * /opt/helen/deploy/coturn/health-check.sh turn.example.com 3478 || systemctl restart helen-coturn
```

## Rotation

To rotate the secret without dropping in-flight allocations:
1. Generate new secret.
2. Update Helen-Server first (`HELEN_TURN_SECRET`) and restart it.
3. Update `turnserver.conf` and `docker compose restart coturn`.
   In-flight allocations live until their TTL (default 600s); new
   credentials get the new secret immediately.

Allowed downtime: zero. Existing calls survive the rotation.

## Capacity

Default limits in `turnserver.conf`:
- 2000 concurrent allocations system-wide
- 10 per user
- 2 Mbps per allocation

For larger deployments increase `total-quota`, `user-quota`, `max-bps`
and provision more relay UDP ports (`min-port`/`max-port`). One
coturn instance comfortably handles ~500 simultaneous video calls
on a 4-core / 8GB VPS at typical Helen bitrates.

## Security notes

- The `static-auth-secret` is the keys-to-the-kingdom. Treat as a
  database password: store in a secret manager, never commit to git.
- Default `denied-peer-ip` blocks RFC1918 + loopback + link-local
  destinations from being relayed — prevents abuse as a generic
  pinhole. Adjust if your traffic legitimately needs to relay to
  internal peers.
- Enable TLS on 5349 with a real cert (Let's Encrypt) for clients
  on networks that block plain UDP/TCP TURN.
