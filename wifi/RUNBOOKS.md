# Helen — Incident Response Runbooks

Operational procedures for the on-call engineer. Each entry assumes
familiarity with the architecture (see `CommClient-Architecture-Blueprint.md`).

---

## RB-1 — Helen-Server is down

**Symptom:** `up{job="helen-server"} == 0` alert fires; users see "Disconnected" indicator; `/api/health` times out.

**Diagnosis:**
1. SSH to host. `ps aux | grep python` — is `run.py` alive?
2. `systemctl status helen-server` (if managed by systemd).
3. `tail -200 /var/log/helen/helen.log` — look for last clean shutdown line vs panic/OOM.
4. `journalctl -u helen-server -n 100 --no-pager`.

**Likely causes:**
- SQLite WAL corruption after host crash → DB locked
- OOM kill → check `dmesg | tail -50`
- Port 3088 already taken → check `ss -tnlp | grep 3088`
- TLS cert expired → check `openssl x509 -in certs/helen.crt -noout -dates`

**Recovery:**
- DB locked: `sqlite3 data/commclient.db ".recover" > recovered.sql && rm data/commclient.db && sqlite3 data/commclient.db < recovered.sql`
- OOM: increase systemd `MemoryLimit=` or add swap
- Port taken: `kill -9 <pid>` of squatter
- Cert: regenerate via `scripts/regen-certs.sh`
- Restart: `systemctl restart helen-server` (or `python run.py` for dev)

**Post-incident:** verify `/api/health` returns 200; smoke-test login + send-message via the seeded `admin1`/`admin1` account.

---

## RB-2 — Federation bridge between two servers is down

**Symptom:** `helen_active_bridges` gauge below expected count; `bridge_disconnected` log on both sides; cross-server users show stale presence.

**Diagnosis:**
1. From server A: `curl https://server-b:3088/api/health` — does B reply?
2. Check shared secret hasn't drifted: `diff /etc/helen/peers.json` between hosts.
3. Inspect TLS handshake: `openssl s_client -connect server-b:3088`.
4. Network: `traceroute server-b`, look for asymmetric routing.

**Likely causes:**
- Shared secret rotated on one side only
- TLS cert expired on the other host
- Firewall rule on one side dropped 3088 inbound
- Clock skew > 5 min → JWT validation fails

**Recovery:**
- Re-distribute peers.json + restart both
- Renew certs
- `ufw allow 3088/tcp`
- `chrony tracking` + force resync via `chronyc makestep`

**Post-incident:** wait 30s for auto-reconnect; verify `/api/peers` lists the other on both sides.

---

## RB-3 — High ICE failure rate

**Symptom:** `helen_ice_failure_ratio > 0.1` alert; users complain "video doesn't connect" or "stays at Connecting forever".

**Diagnosis:**
1. Pull last 30 min of `ice_failed` logs: `journalctl -u helen-server | grep ice_failed`.
2. Group by user IP / network — is it a specific subnet?
3. Check TURN reachability from a test client: `turnutils_uclient -u test -w test -p 3478 turn.example.com`.
4. Open `chrome://webrtc-internals` on a failing client during a call; check candidate-pair stats.

**Likely causes:**
- TURN server not reachable (firewall on TURN host)
- TURN credentials expired (server-side secret rotation)
- Symmetric NAT both sides + TURN unconfigured
- IPv6-only network (Helen's ICE config is IPv4-biased)

**Recovery:**
- Restart coturn: `systemctl restart coturn`
- Rotate TURN secret + restart Helen-Server (re-fetch `/api/turn/ice-config`)
- Verify TURN-only fallback works: have a user enable `iceTransportPolicy: 'relay'` in DevTools and retry

**Post-incident:** after fix, ratio should drop below 5% within 10 min.

---

## RB-4 — Database (SQLite) unavailable / slow

**Symptom:** 503 errors from `/api/channels`; `database_error` log entries; `helen_database_query_seconds` p99 > 2s.

**Diagnosis:**
1. `df -h /var/lib/helen/` — is disk full?
2. `iostat -x 1 5` — is disk saturated?
3. `sqlite3 data/commclient.db "PRAGMA integrity_check;"` — corruption?
4. Check WAL file size: `ls -la data/commclient.db-wal`. >100MB suggests stuck checkpoint.

**Likely causes:**
- Disk full
- WAL not checkpointing (long-running read transaction holds it)
- DB file corruption from kill -9 mid-write
- Mass concurrent writes from group-call burst

**Recovery:**
- Free disk space (rotate logs: `journalctl --vacuum-time=7d`)
- Force checkpoint: `sqlite3 data/commclient.db "PRAGMA wal_checkpoint(TRUNCATE);"`
- For corruption: see RB-1 recovery steps
- For burst load: enable connection pool limit in `app/db/session.py`

**Long-term:** plan migration to PostgreSQL when concurrent users > 100.

---

## RB-5 — TURN service down

**Symptom:** `helen_turn_usage_ratio` drops to 0; specific users report "video keeps reconnecting" especially on mobile/cellular.

**Diagnosis:**
1. `systemctl status coturn`
2. `nc -uvz turn.example.com 3478` from a client network
3. `tail /var/log/turnserver/turnserver.log`

**Likely causes:**
- coturn process crashed
- coturn config has stale realm or shared-secret
- ISP blocking UDP 3478 (TCP TURN on 5349 still works)

**Recovery:**
- `systemctl restart coturn`
- Verify Helen-Server's TURN secret matches: `grep -E 'static-auth-secret' /etc/turnserver.conf` vs `app/services/turn_service.py`
- Add TCP TURN on 5349 if UDP is blocked

**Post-incident:** force a re-fetch on all desktop clients by bumping the server's `TURN_VERSION` env var.

---

## RB-6 — Room state inconsistency across servers

**Symptom:** users on server-A see member X in room R; users on server-B don't. `helen_room_member_diff_across_servers > 0`.

**Diagnosis:**
1. Pull room state from each server: `curl /api/channels/<id>` on A and B.
2. Compare member arrays — find missing user.
3. Check audit log: `grep <user_id>.*joined`.

**Likely causes:**
- Federation event missed during a brief partition
- Race in `placement.py` — both servers thought they owned the room
- Stale cache in one server

**Recovery:**
- Force resync on the lagging server: `curl -X POST -H "Authorization: Bearer <admin>" /api/admin/federation/resync-room/<channel_id>`
- (If endpoint doesn't exist yet — server restart triggers full resync at boot)

**Long-term:** implement gossip-based reconciliation with last-write-wins by timestamp.

---

## RB-7 — Memory leak (RSS climbing on server)

**Symptom:** `helen_server_memory_bytes` rises monotonically over hours; eventual OOM kill.

**Diagnosis:**
1. `py-spy dump --pid <helen-server-pid>` — see what async tasks are stuck
2. Check active call count vs RSS — is it proportional or detached?
3. Look for leaked WebSocket sessions: `helen_websocket_connections` should track real users

**Likely causes:**
- WebSocket sessions not cleaned on disconnect
- RTCPeerConnection refs held by event listeners
- structlog accumulating context dicts

**Recovery (immediate):**
- Schedule rolling restart during low traffic (every 24h)
- Add `python -X tracemalloc=10` to startup args, capture snapshot, diff

**Long-term:** add explicit cleanup hooks on `socket.disconnect`, weakref'd handlers.

---

## RB-8 — Abnormal disconnect spike

**Symptom:** `rate(helen_websocket_disconnects_total[5m])` > 10× baseline; users report "I keep getting kicked out".

**Diagnosis:**
1. Inspect server logs grouped by reason: `journalctl | awk '/disconnect/ { print $NF }' | sort | uniq -c`
2. Check NIC errors: `ifconfig` → RX-DRP, TX-ERR
3. Check load balancer logs (if using Caddy/nginx)
4. Look at JWT expiration spike: a coordinated re-auth storm flips many sockets

**Likely causes:**
- Network instability (ISP issue, switch flap)
- JWT secret rotated → all sessions invalidated simultaneously
- Load balancer health-check too aggressive
- DDoS (rare on LAN)

**Recovery:**
- Network: contact ISP, swap NIC if hardware
- JWT: stagger rotation; provide grace period
- LB: relax health-check interval to 30s

---

## Appendix — Useful commands

```bash
# Live tail of structured logs
journalctl -u helen-server -f --no-pager | jq

# Active call count
curl -s -H "Authorization: Bearer $TOK" http://localhost:3088/api/admin/active-calls | jq length

# Room placement summary
curl -s -H "Authorization: Bearer $TOK" http://localhost:3088/api/admin/rooms | jq '[.[] | .assigned_node_id] | group_by(.) | map({(.[0]): length})'

# Force checkpoint of WAL
sqlite3 data/commclient.db "PRAGMA wal_checkpoint(TRUNCATE);"

# Rotate logs (>30 days)
journalctl --vacuum-time=30d

# WebRTC stats from a Chrome client
# (Open chrome://webrtc-internals during a call → Save Stats As → forward to support)
```

---

## Escalation

- L1: on-call engineer (15-min response SLA)
- L2: backend lead (1-hour SLA for critical)
- L3: product owner (4-hour SLA for severe)

For data-loss or security incidents, page L3 directly.
