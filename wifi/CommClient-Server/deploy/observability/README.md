# Helen-Server — Observability Stack

Self-hosted, batteries-included stack:

* **Prometheus** — metric scraping + Alertmanager glue
* **Alertmanager** — routes/escalates alerts
* **Grafana** — dashboards + log/trace viewer
* **Loki** — log storage
* **Promtail** — log shipper (or use the in-process Loki handler)
* **Tempo** — distributed trace storage (OTLP-compatible)

## Bring up

```bash
docker compose \
  -f deploy/observability/docker-compose.observability.yml \
  up -d
```

Then point Helen at the OTel endpoint:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318
export OTEL_SERVICE_NAME=helen-server
export LOKI_URL=http://loki:3100
```

The Python SDKs are imported lazily — if any of these packages are not
installed, the corresponding subsystem simply turns off.

## Required pip deps (all optional)

```
prometheus-client>=0.20
opentelemetry-sdk>=1.27
opentelemetry-exporter-otlp-proto-http>=1.27
opentelemetry-instrumentation-fastapi>=0.48b0
opentelemetry-instrumentation-sqlalchemy>=0.48b0
opentelemetry-instrumentation-httpx>=0.48b0
opentelemetry-instrumentation-redis>=0.48b0
```

## URLs (default ports)

| Service       | URL                            |
|---------------|--------------------------------|
| Grafana       | http://localhost:3001          |
| Prometheus    | http://localhost:9090          |
| Alertmanager  | http://localhost:9093          |
| Loki          | http://localhost:3100          |
| Tempo (HTTP)  | http://localhost:3200          |
| OTLP HTTP     | http://tempo:4318/v1/traces    |
| OTLP gRPC     | tempo:4317                     |

## Dashboards

Three are auto-provisioned under the "Helen" folder:

* **Helen — Overview** — top-level RED metrics + cluster state
* **Helen — Messaging** — sockets, bridges, AI, webhooks
* **Helen — System** — JWT, backups, IDS, logs panel

## Alerting

Default alert rules live in `prometheus/alert_rules.yml`. Edit and
hot-reload Prometheus:

```bash
docker exec -it deploy-prometheus-1 \
  killall -HUP prometheus
```

In-process structured alerts (`app.observability.structured_alerts`)
evaluate every 15s and dispatch to webhook / email / cluster pubsub —
manage them at `/api/admin/observability/alerts/rules`.

## Wiring it into the app

In `app/main.py`:

```python
from app.observability.otel_tracing import setup_tracing
from app.observability.log_shipper import attach_log_shipper
from app.observability.structured_alerts import get_alerts_engine

@app.on_event("startup")
async def _obs_startup():
    setup_tracing(app)
    attach_log_shipper()
    await get_alerts_engine().start()

@app.on_event("shutdown")
async def _obs_shutdown():
    await get_alerts_engine().stop()
```

The Prometheus exporter is wired automatically by mounting the
existing `/metrics` route; see `app.api.routes.metrics`.
