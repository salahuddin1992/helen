# Helen Edge Node

Lightweight geo-distributed worker. Sits between users and the origin
control plane; offloads CPU-bound work (thumbnails, spam filtering,
validation), caches frequently-read data, and lets clients hit the
nearest endpoint for low-latency message delivery.

## Quick start

1. Provision a small VM in your target region (1 vCPU / 1 GB RAM is
   plenty for a starter node).
2. Copy `docker-compose.edge.yml` and an `.env.edge` file. Sample env:

   ```env
   HELEN_EDGE_NODE_ID=edge-fra-01
   HELEN_EDGE_REGION=eu-central-1
   HELEN_EDGE_PUBLIC_URL=https://edge-fra-01.helen.example.org
   HELEN_EDGE_ORIGIN_URL=https://helen.example.org
   HELEN_EDGE_NODE_TOKEN=<shared-secret-with-origin>
   HELEN_EDGE_GEO_LAT=50.11
   HELEN_EDGE_GEO_LNG=8.68
   HELEN_EDGE_CITY=Frankfurt
   HELEN_EDGE_COUNTRY=DE
   HELEN_EDGE_PORT=8089
   ```

3. (Optional) Drop a MaxMind GeoLite2-City.mmdb into the `edge-data`
   volume so that GeoIP lookups work on the worker too. The origin's
   geo_router can run without it.

4. Register the node with the origin admin:

   ```bash
   curl -X POST https://helen.example.org/api/admin/edge/nodes \
       -H "Authorization: Bearer $ADMIN_TOKEN" \
       -H "Content-Type: application/json" \
       -d '{
            "node_id":       "edge-fra-01",
            "region":        "eu-central-1",
            "city":          "Frankfurt",
            "country":       "DE",
            "advertise_url": "https://edge-fra-01.helen.example.org",
            "public_url":    "https://edge-fra-01.helen.example.org",
            "geo_lat":       50.11,
            "geo_lng":       8.68
           }'
   ```

5. Bring the stack up:

   ```bash
   docker compose -f deploy/edge/docker-compose.edge.yml \
       --env-file .env.edge up -d
   ```

## Operations

* Health check: `GET /api/edge/health`
* Latency steering: origin periodically probes each registered node;
  view the matrix at `Admin → Edge → Latency`.
* Data residency: per-workspace policy enforced on the origin before
  steering. See `Admin → Edge → Region Policies`.
* TLS: terminate at your load balancer / reverse proxy in front of the
  container. The container itself listens on plain HTTP.

## Scaling

* Stateless. Just spin up more compose stacks in more regions.
* Cache invalidation propagates via cluster pubsub (Redis or HTTP
  fan-out), which the origin already runs.
* No DB on the edge — workers are pure functions. The origin is the
  single source of truth.

## Sizing

| Component          | CPU | RAM   | Notes                            |
|--------------------|-----|-------|----------------------------------|
| edge-worker        | 1   | 256MB | Per node. CPU spikes on thumbs.  |
| edge-redis         | 0.5 | 256MB | Cache. Drop if no Redis usage.   |

## Security

* `HELEN_EDGE_NODE_TOKEN` is the shared secret that authenticates the
  edge worker to the origin on both heartbeat and websocket sync.
* Rotate the token regularly. The origin also signs every cross-edge
  pubsub event via the cluster bus.
