# Helen-Server — Cluster Deployment

Three-node high-availability deployment with shared Postgres, Redis,
and a sticky reverse proxy.

```
            ┌──────────────┐
            │   nginx /    │ ip_hash on /socket.io
            │   HAProxy    │
            └──────┬───────┘
       ┌──────────┼──────────┐
       ▼          ▼          ▼
   ┌────────┐ ┌────────┐ ┌────────┐
   │ helen1 │ │ helen2 │ │ helen3 │
   └────┬───┘ └────┬───┘ └────┬───┘
        │          │          │
        ├──────────┼──────────┤
        ▼          ▼          ▼
      Postgres  ←  Redis  ←  Pub/Sub
```

## 1. Required environment

| Variable                    | Required | Example                                   |
|-----------------------------|----------|-------------------------------------------|
| `HELEN_JWT_SECRET`          | yes      | `openssl rand -hex 64`                    |
| `DATABASE_URL`              | yes      | `postgresql+asyncpg://helen:helen@db/...` |
| `REDIS_URL`                 | yes      | `redis://redis:6379/0`                    |
| `COMMCLIENT_CLUSTER_ID`     | yes      | `helen-prod`                              |
| `HELEN_ADVERTISE_URL`       | yes      | Per-node URL the other nodes can reach    |
| `SESSION_STORE_BACKEND`     | no       | `redis` (default if `REDIS_URL` set)      |
| `LEADER_ELECTION_BACKEND`   | no       | `redis`                                   |

## 2. Bring up the stack

```bash
export HELEN_JWT_SECRET=$(openssl rand -hex 64)
docker compose -f docker-compose.cluster.yml up -d
docker compose -f docker-compose.cluster.yml logs -f helen1
```

The first node to boot wins the leader election; the others become
replicas. Subsequent `helen-server` restarts re-elect transparently
within `LEADER_LEASE_TTL_SECONDS / 2` (default 7.5 s).

## 3. Scaling out

Add more replicas:

```bash
docker compose -f docker-compose.cluster.yml up -d --scale helen=5
```

Then regenerate the nginx upstream block from any node:

```bash
curl -H "Authorization: Bearer $TOKEN" \
     https://helen.example.com/api/admin/cluster/nginx-upstream \
     | jq -r .config > /etc/nginx/conf.d/helen-upstream.conf
sudo nginx -s reload
```

(Same for HAProxy via `/api/admin/cluster/haproxy-backend`.)

## 4. Graceful drain

To roll out a new build without dropping connections:

```bash
# Mark node draining → LB stops sending new connections
curl -X POST -H "Authorization: Bearer $TOKEN" \
     https://helen.example.com/api/admin/cluster/nodes/$NODE_ID/drain

# Wait for in-flight calls to end (watch /api/admin/cluster/health)
# Then stop the container
docker compose -f docker-compose.cluster.yml stop helen1

# Pull new image, start, repeat for helen2 / helen3
```

## 5. Switching the reverse proxy

The repo ships both nginx and HAProxy configs. To use HAProxy instead:

```bash
docker run -d --name helen-lb \
  -p 80:80 -p 443:443 -p 8404:8404 \
  --network helen-cluster \
  -v $PWD/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro \
  -v $PWD/certs:/etc/ssl/helen:ro \
  haproxy:2.9-alpine
```

The HAProxy stats UI is on `:8404/stats`.

## 6. Sticky behaviour

* **nginx** uses `ip_hash` — cheap and works out-of-the-box; clients
  with the same IP always land on the same upstream.
* **HAProxy** uses consistent-hash `balance source` plus an
  `X-User-Id` stick-table for cookie-less affinity when the client
  injects a user id header (the Helen JS clients do this).

The internal sticky-router (`app.services.cluster.sticky_router`)
exposes the same ring so socket.io message delivery picks the
right peer when broadcasting via pub/sub.

## 7. Failure semantics

| Failure                     | Behaviour                                                            |
|-----------------------------|----------------------------------------------------------------------|
| One node crashes            | Sessions on it are pushed back to the LB → reconnect to another node |
| LB crashes                  | Stand up a backup LB (active/passive) — clients use DNS              |
| Postgres primary fails      | Promote replica; Helen reconnects on next request                    |
| Redis fails                 | Session store + pub/sub degrade to SQL/HTTP fan-out automatically    |

## 8. Monitoring

* `GET /api/admin/cluster/health` — JSON snapshot
* `GET /api/admin/cluster/nodes` — list of nodes
* `GET /api/admin/cluster/leader` — current leader and term
* Admin module: `admin/modules/cluster.html` — topology + live timeline
