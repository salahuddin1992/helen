# Helen — Kubernetes Operator Runbook

Production operational guide for running Helen / CommClient on Kubernetes via the bundled Helm chart.

## 1. Prerequisites

- Kubernetes 1.27+ cluster (any distribution)
- Helm 3.12+
- `kubectl` configured
- Private container registry on LAN (e.g., `registry.helen.lan`) populated with the Helen images:
  - `helen/server:1.0.0`
  - `helen/router:1.0.0`
  - `helen/rendezvous:1.0.0` (only if hybrid LAN/WAN deployment)
- A storage class supporting `ReadWriteOnce` (e.g., `local-path`, `longhorn`, `ceph-rbd`, `openebs-jiva`)
- Optional: cert-manager for automatic TLS, Prometheus Operator for metrics scraping
- Optional: external-secrets / sealed-secrets / Vault for production secret management

## 2. Quick install (development)

```bash
helm install helen ./deploy/helm/helen \
    --namespace helen \
    --create-namespace \
    --set global.imageRegistry=registry.helen.lan \
    --set server.ingress.hostname=helen.lan
```

Within 60 seconds:

```bash
kubectl -n helen get pods
kubectl -n helen port-forward svc/helen-server 3000:3000
# Then open http://127.0.0.1:3000/
```

## 3. Production install (hardened)

### 3.1 Create namespace + image-pull secret

```bash
kubectl create namespace helen
kubectl -n helen create secret docker-registry helen-registry-creds \
    --docker-server=registry.helen.lan \
    --docker-username=helen-puller \
    --docker-password='<password>' \
    --docker-email=ops@helen.lan
```

### 3.2 Provide secrets out-of-band

If you DON'T want Helm to auto-generate a JWT secret:

```bash
kubectl -n helen create secret generic helen-jwt-secret \
    --from-literal=secret="$(openssl rand -base64 64)"
```

Override in values:

```yaml
auth:
  autoCreateJwtSecret: false
  jwtSecretName: helen-jwt-secret
```

### 3.3 Install

```bash
helm install helen ./deploy/helm/helen \
    --namespace helen \
    --values production-values.yaml
```

Example `production-values.yaml`:

```yaml
global:
  imageRegistry: registry.helen.lan
  imagePullSecrets:
    - name: helen-registry-creds
  storageClass: longhorn
  airgap: true

server:
  replicas: 3
  resources:
    requests: {cpu: 1000m, memory: 2Gi}
    limits: {cpu: 4000m, memory: 8Gi}
  autoscaling:
    enabled: true
    minReplicas: 3
    maxReplicas: 12
  ingress:
    hostname: helen.example.local
    annotations:
      cert-manager.io/cluster-issuer: helen-internal-ca
    tls:
      enabled: true
      secretName: helen-tls

router:
  replicas: 5
  resources:
    requests: {cpu: 500m, memory: 1Gi}
    limits: {cpu: 2000m, memory: 4Gi}

postgres:
  enabled: false  # use external managed postgres
externalPostgres:
  url: "postgresql+asyncpg://helen:$(POSTGRES_PASSWORD)@pg-primary.helen.lan:5432/helen"

monitoring:
  serviceMonitor:
    enabled: true
  prometheusRule:
    enabled: true
```

## 4. Day-2 operations

### 4.1 Apply database migrations

Migrations run automatically on startup via the embedded Alembic chain.
To apply manually:

```bash
kubectl -n helen exec -it deploy/helen-server -- alembic upgrade head
```

To preview SQL without applying:

```bash
kubectl -n helen exec -it deploy/helen-server -- alembic upgrade head --sql > migration-preview.sql
```

### 4.2 Seed demo data

```bash
kubectl -n helen exec -it deploy/helen-server -- python -m app.cli.seed_admin_panels --update
```

To wipe and reseed:

```bash
kubectl -n helen exec -it deploy/helen-server -- python -m app.cli.seed_admin_panels --reset
```

### 4.3 Run onboarding wizard

Browse to `https://<hostname>/admin/modules/onboarding_wizard.html` and complete all 14 steps to bootstrap the operator account, TLS cert, license, and initial tenant.

### 4.4 Trigger backup

```bash
kubectl -n helen exec -it deploy/helen-server -- curl -X POST \
    -H "Authorization: Bearer $TOKEN" \
    http://localhost:3000/api/admin/dr/backups \
    -d '{"policy_id": "nightly-full"}'
```

### 4.5 Verify audit chain integrity

```bash
kubectl -n helen exec -it deploy/helen-server -- curl -X POST \
    -H "Authorization: Bearer $TOKEN" \
    http://localhost:3000/api/admin/audit/verify
```

### 4.6 Rolling restart

```bash
kubectl -n helen rollout restart deploy/helen-server
kubectl -n helen rollout status deploy/helen-server --timeout=300s
```

### 4.7 Scale server

```bash
kubectl -n helen scale deploy/helen-server --replicas=6
# OR via helm
helm -n helen upgrade helen ./deploy/helm/helen --reuse-values --set server.replicas=6
```

## 5. Disaster recovery

### 5.1 Backup restore from artifact

Use the DR Console UI (`/admin/modules/dr_console.html`) → Restore Wizard, or:

```bash
kubectl -n helen exec -it deploy/helen-server -- curl -X POST \
    -H "Authorization: Bearer $TOKEN" \
    http://localhost:3000/api/admin/dr/backups/<backup-id>/restore \
    -H 'Content-Type: application/json' \
    -d '{"target": "sandbox", "scope": "full", "confirmation": "RESTORE", "reason": "DR drill"}'
```

### 5.2 Full cluster rebuild

1. Recreate the cluster.
2. Reinstall Helm chart with same release name.
3. Re-attach existing PVCs OR restore from off-cluster backup of `/data` directory.
4. Run `alembic upgrade head` (idempotent).
5. Login with operator credentials, verify audit chain.

## 6. Monitoring

### 6.1 Prometheus

If `monitoring.serviceMonitor.enabled=true`, Helen exposes metrics at:

```
GET /api/admin/observability/metrics
```

Key metrics:

| Name | Type | Description |
|------|------|-------------|
| `helen_requests_total` | counter | Total HTTP requests |
| `helen_requests_failed_total` | counter | Failed (5xx) requests |
| `helen_active_connections` | gauge | Active client connections |
| `helen_active_calls` | gauge | Calls in progress |
| `helen_audit_chain_verify_status` | gauge | 1=ok, 0=tampered |
| `helen_dr_last_backup_timestamp_seconds` | gauge | Unix ts of last good backup |
| `helen_federation_peers_healthy` | gauge | Healthy federation peers |
| `helen_federation_replication_lag_seconds` | gauge | Worst-case replication lag |

### 6.2 Alerting rules (already shipped)

- `HelenServerDown` — pod unreachable for 2m → critical
- `HelenHighErrorRate` — >5% errors over 5m → warning
- `HelenAuditChainTamper` — chain verify failed → critical
- `HelenBackupOverdue` — last successful backup >24h ago → warning

## 7. Troubleshooting

### 7.1 Pods stuck in Pending

```bash
kubectl -n helen describe pod <name>
```

Common causes: PVC not bound (storage class missing), image pull failure (registry creds), nodeSelector mismatch.

### 7.2 Database connection failures

```bash
kubectl -n helen logs deploy/helen-server | grep -i 'database\|sqlalchemy\|connect'
```

Verify `DATABASE_URL` env, network policy egress to postgres, postgres readiness.

### 7.3 JWT secret mismatch across pods

Rolling restart all pods after rotating JWT secret:

```bash
kubectl -n helen rollout restart deploy/helen-server deploy/helen-router
```

### 7.4 Audit chain verification fails

CRITICAL alert. Steps:

1. Take a manual snapshot of `/data/audit-chain.db` immediately.
2. Stop new writes: scale server to 0 replicas.
3. Compare snapshot with latest backup → identify divergence point.
4. Restore last known-good chain from DR.
5. File security incident.

### 7.5 Federation peer keeps quarantining

```bash
kubectl -n helen exec -it deploy/helen-server -- curl \
    -H "Authorization: Bearer $TOKEN" \
    http://localhost:3000/api/admin/federation/peers/<peer-id>/audit | head -100
```

Common causes: cert expiry, time skew >500ms, replication conflict, bandwidth shaper too aggressive.

## 8. Upgrade procedure

```bash
# 1. Read the release notes for breaking changes.
# 2. Backup the audit chain + DB:
kubectl -n helen exec -it deploy/helen-server -- python -m app.cli.snapshot --all --output /tmp/pre-upgrade.tgz
kubectl -n helen cp helen/helen-server-xxx:/tmp/pre-upgrade.tgz ./pre-upgrade.tgz

# 3. Upgrade chart:
helm -n helen upgrade helen ./deploy/helm/helen \
    --reuse-values \
    --set server.image.tag=1.1.0 \
    --set router.image.tag=1.1.0

# 4. Watch rollout:
kubectl -n helen rollout status deploy/helen-server --timeout=600s

# 5. Verify health post-upgrade:
kubectl -n helen exec -it deploy/helen-server -- curl -s http://localhost:3000/api/admin/health
```

## 9. Uninstall

```bash
# Delete chart but keep PVCs (default Helm behavior):
helm -n helen uninstall helen

# Delete everything including PVCs and Secrets:
kubectl delete namespace helen
```

## 10. Reference

- API documentation: `/api/docs` (Swagger UI) when `server.env.HELEN_DOCS_ENABLED=1`
- OpenAPI spec: `docs/openapi/helen_admin_v1.json`
- Endpoint inventory: `docs/openapi/ENDPOINTS.md`
- Architecture diagrams: `docs/MESH_ARCHITECTURE.md`, `docs/NETWORK_TOPOLOGY.md`
- Migration chain: `migrations/versions/` (chained off `helen_onboarding_addon` as current head)
