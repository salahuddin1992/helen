# Helen-Rendezvous — Cluster / HA Operator Guide

This document covers operating Helen-Rendezvous in **multi-instance** mode
with Redis-backed shared state. The single-process mode in `main.py` keeps
working unchanged — cluster mode is opt-in via env vars.

---

## 1. When to use cluster mode

Use cluster mode when **any** of these is true:

| Condition                                      | Why it forces cluster mode                                |
|------------------------------------------------|-----------------------------------------------------------|
| > 500 simultaneous Helen-Server tunnels         | Single process saturates one event loop / one core        |
| Multi-region deployment                         | Need a rendezvous per region but a unified registry       |
| Zero-downtime rolling deploys required          | LB drains one instance while others keep serving          |
| Sub-second failover SLA                         | Sentinel-promoted Redis + multi-instance survives any one VM dying |
| Operators want centralized observability        | `/admin/cluster/instances` gives a live roster            |

Below 500 tunnels and with a single region, the in-memory default is faster
and operationally simpler. Don’t introduce Redis just because.

---

## 2. Deployment topology

```
                ┌────────────────────────────────────────────────────┐
                │                  L7 / L4 Load Balancer             │
                │           (nginx, HAProxy, Cloud LB, etc.)         │
                └───┬──────────────┬──────────────┬──────────────────┘
                    │              │              │
            ┌───────▼──────┐ ┌─────▼────────┐ ┌───▼──────────┐
            │ rendezvous-1 │ │ rendezvous-2 │ │ rendezvous-3 │
            └───┬──────┬───┘ └─────┬────────┘ └──────┬───────┘
                │      │           │                 │
                │      └───────────┴─────────────────┘
                │             pub/sub :  rendezvous:events
                ▼
        ┌──────────────────────────────────────────┐
        │  Redis (standalone | sentinel | cluster) │
        │   tunnel:*  signal:*  lock:*  instance:* │
        └──────────────────────────────────────────┘
```

**LB hint:** sticky sessions are *not* required. A Helen-Server's WebSocket
will always land on the instance it first connected to (it’s a long-lived
socket); external client HTTP requests can hit any instance because the
cross-instance relay forwards transparently.

---

## 3. Configuration

All cluster behaviour is selected by env vars.

| Env                                                | Default              | Notes                                              |
|----------------------------------------------------|----------------------|----------------------------------------------------|
| `HELEN_RENDEZVOUS_STORAGE`                         | `memory`             | `memory` \| `redis` \| `redis-sentinel` \| `redis-cluster` |
| `HELEN_RENDEZVOUS_REDIS_URL`                       | `redis://localhost:6379/0` | Used when storage=`redis` (TLS via `rediss://`)   |
| `HELEN_RENDEZVOUS_REDIS_USERNAME`                  | unset                | Redis 6+ ACL user                                  |
| `HELEN_RENDEZVOUS_REDIS_PASSWORD`                  | unset                | Sets `password=` on the client                     |
| `HELEN_RENDEZVOUS_REDIS_TLS`                       | `0`                  | `1` enables `ssl_cert_reqs=required`               |
| `HELEN_RENDEZVOUS_REDIS_SENTINELS`                 | unset                | `host:port,host:port,...`                          |
| `HELEN_RENDEZVOUS_REDIS_SENTINEL_MASTER`           | `mymaster`           | Master set name                                    |
| `HELEN_RENDEZVOUS_REDIS_SENTINEL_PASSWORD`         | unset                | Sentinel auth                                      |
| `HELEN_RENDEZVOUS_REDIS_CLUSTER_NODES`             | unset                | `host:port,host:port,...`                          |
| `HELEN_RENDEZVOUS_REDIS_KEY_PREFIX`                | `""`                 | Multi-tenant Redis isolation                       |
| `HELEN_RENDEZVOUS_REDIS_EVENTS_CHANNEL`            | `rendezvous:events`  | Pub/sub channel name                               |
| `HELEN_RENDEZVOUS_INSTANCE_ID`                     | auto                 | Override if your LB uses it for hashing            |
| `HELEN_RENDEZVOUS_PUBLIC_IP`                       | auto                 | What we publish into the roster                    |
| `HELEN_RENDEZVOUS_HEARTBEAT_INTERVAL`              | `5`                  | Heartbeat period (sec)                             |
| `HELEN_RENDEZVOUS_HEARTBEAT_TTL`                   | `15`                 | Roster TTL — must be ≥ 2× interval                 |
| `HELEN_RENDEZVOUS_TUNNEL_TTL`                      | `60`                 | Shared tunnel index TTL (refreshed by the owner)   |
| `HELEN_RENDEZVOUS_SIGNAL_TTL`                      | `300`                | Shared signal entry TTL                            |
| `HELEN_RENDEZVOUS_XINST_TIMEOUT`                   | `25`                 | Cross-instance RPC timeout                         |
| `HELEN_RENDEZVOUS_TOKEN`                           | required             | Same as single-instance mode                       |

### Minimum cluster config

```bash
export HELEN_RENDEZVOUS_TOKEN="$(openssl rand -hex 32)"
export HELEN_RENDEZVOUS_STORAGE=redis
export HELEN_RENDEZVOUS_REDIS_URL="redis://:secret@redis-host:6379/0"
```

That’s it. Boot two or more instances behind a load balancer and they
auto-discover each other through Redis.

---

## 4. Redis sizing

For a baseline workload (~10k tunnels, ~10k signals, ~100 ops/sec):

| Resource         | Recommended                                  |
|------------------|----------------------------------------------|
| Memory           | 512 MB (~16 KB / tunnel + ~512 B / signal)   |
| CPU              | 1 vCPU is enough; Redis is single-threaded   |
| Persistence      | AOF `appendfsync everysec`                   |
| HA               | Sentinel quorum (3 sentinels, 1 master + 1 replica) |
| Network          | < 1 ms RTT to rendezvous instances           |

For 100k tunnels: 2–4 GB, AOF on a fast SSD, consider Cluster mode if a
single Redis node can’t fit memory.

---

## 5. Failover behaviour

| Failure                                  | Outcome                                                         |
|------------------------------------------|-----------------------------------------------------------------|
| One rendezvous instance crashes          | LB drops it, surviving instances serve all traffic. New WS connections land on others; client requests for tunnels owned by the dead instance return 502 once their TTL expires (≤ 60 s by default). |
| Redis becomes unreachable                | Backend returns degraded health. New tunnel/signal writes return False but `lookup_*` falls back to local in-process state. When Redis returns, normal operation resumes — no restart required. |
| Network partition between regions        | Each side sees only its instances. Existing tunnels stay up. After partition heals, heartbeats re-sync the roster automatically. |
| Redis master fails (Sentinel mode)       | Sentinel elects a new master; redis-py reconnects; ≤ 5 s pause for in-flight writes. |

---

## 6. Migration from single-instance

Existing single-instance deployments need **no code changes**. The new
release boots in `memory` mode by default. To migrate:

1. Stand up Redis (single node is fine for cutover).
2. Roll out the new release everywhere — still single-instance, still memory.
3. Set `HELEN_RENDEZVOUS_STORAGE=redis` + `HELEN_RENDEZVOUS_REDIS_URL` on
   one instance and restart it. Watch `/admin/cluster/instances` — you’ll
   see it alone in the roster.
4. Bring up the second, third instances with the same env. They show up in
   the roster within one heartbeat interval.
5. Point your LB at the new instance pool.
6. Done — old single-instance can be drained and decommissioned.

There is no state-migration step: tunnels are short-lived (re-registered by
Helen-Servers on reconnect) and signals are TTL-bounded (5 min by default).

---

## 7. Observability

| Endpoint                                  | Returns                                              |
|-------------------------------------------|------------------------------------------------------|
| `GET /admin/cluster/instances?token=...`  | Live roster from Redis                               |
| `GET /admin/cluster/stats?token=...`      | Local + cluster-wide tunnel/signal counts            |
| `GET /admin/cluster/health?token=...`     | Backend health + relay pump stats                    |
| `GET /admin/cluster/tunnels?token=...`    | Paginated shared tunnel index                        |
| `GET /admin/cluster/relay/stats?token=...`| `sent / received / responses_matched / unhandled`    |

The existing `GET /health` remains the LB probe path — it returns 200 in
any healthy mode.

---

## 8. Security

- TLS to Redis: use `rediss://` URLs, set `HELEN_RENDEZVOUS_REDIS_TLS=1`.
- AUTH: `HELEN_RENDEZVOUS_REDIS_PASSWORD` (+ `_USERNAME` for ACLs).
- Lock down Redis at the network layer too. The rendezvous bootstrap token
  protects the public API; Redis is not protected by it.
- Per-tenant isolation: set a distinct `HELEN_RENDEZVOUS_REDIS_KEY_PREFIX`
  per deployment if you share one Redis between environments.

---

## 9. Troubleshooting

| Symptom                                                 | Likely cause / fix                                                              |
|---------------------------------------------------------|---------------------------------------------------------------------------------|
| Instance not in `/admin/cluster/instances`              | Heartbeat failing — check Redis connectivity and `redis_op_giveup` warnings     |
| External clients get 404 on a valid tunnel              | TTL expired — Helen-Server hasn’t refreshed; check WS keepalive on server side  |
| Cross-instance HTTP requests time out                   | Owning instance crashed; clients should retry — relay will return 504           |
| `health.backend.status == "degraded"`                   | Transient Redis errors — count `consecutive_failures`, escalate if persistent   |
| `pub/sub` delivery flaky after Redis restart            | redis-py reconnects but pump may have stalled; `cluster_stats.relay_stats.received` will resume |
