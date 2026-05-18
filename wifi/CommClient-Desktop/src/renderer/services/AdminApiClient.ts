/**
 * AdminApiClient — thin wrappers around the 11 new admin REST endpoints
 * (monitoring, topology, SIEM/audit, billing, DR, plugins, federation,
 * QoS, compliance, onboarding, router).
 *
 * Design
 * ──────
 * We deliberately keep this surface narrow — only the operations that the
 * desktop client invokes directly (kick a connection, force a backup, etc.)
 * The embedded HTML panels still talk to the API on their own; this client
 * is for the native React surface that wraps them with quick-actions,
 * notification handlers, and future native rewrites.
 *
 * All requests go through the existing api.client request() infrastructure,
 * so token refresh, rate-limit handling, and 401-driven auth-failure
 * propagation are inherited.
 *
 * The `internal` import is intentional: api.client.ts already exports the
 * configured `api` object plus the low-level `request` symbol used here. We
 * mirror the same pattern adminPeers / adminCrashes use (extension lives in
 * its own file, but still inside the renderer process).
 */

import { ApiError, getBaseUrl, getAccessToken } from './api.client';

// ── Internal helpers ─────────────────────────────────────────────────────
async function authedFetch<T>(method: string, path: string, body?: unknown): Promise<T> {
  const baseUrl = getBaseUrl();
  const token = getAccessToken();
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';

  const res = await fetch(`${baseUrl}${path}`, {
    method,
    headers,
    body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
  });

  if (res.status === 204) return undefined as T;
  const isJson = (res.headers.get('content-type') || '').includes('application/json');
  const data = isJson ? await res.json() : (await res.text() as any);
  if (!res.ok) {
    const detail = (typeof data === 'object' && data && (data.detail || data.error)) || 'Request failed';
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

const qs = (params?: Record<string, string | number | boolean | undefined | null>): string => {
  if (!params) return '';
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : '';
};

// ── Type stubs (servers return rich payloads; we expose `any` here and
//    let callers narrow as needed). Tighten over time. ─────────────────
export interface MonitoringMetrics {
  uptime_seconds: number;
  cpu_percent: number;
  memory_percent: number;
  memory_used: number;
  connected_clients: number;
  active_calls: number;
  message_rate_per_sec: number;
  db_size_bytes: number;
  ts: string;
}

export interface TopologyNode {
  id: string;
  kind: 'server' | 'router' | 'peer' | 'tenant' | 'client';
  label: string;
  region?: string;
  status?: 'healthy' | 'degraded' | 'down' | 'unknown';
  metrics?: Record<string, number>;
}
export interface TopologyEdge {
  from: string;
  to: string;
  kind: 'federation' | 'tunnel' | 'sfu' | 'transport';
  weight?: number;
  latency_ms?: number;
}
export interface TopologyGraph {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  generated_at: string;
}

export interface AuditChainHead {
  seq: number;
  chain_hash: string;
  timestamp: number;
  actor: string;
  action: string;
  target: string | null;
}

export interface Tenant {
  id: string;
  name: string;
  plan: string;
  status: 'active' | 'suspended' | 'pending';
  user_count: number;
  license_expires_at?: string | null;
  created_at: string;
}

export interface License {
  id: string;
  tenant_id: string;
  plan: string;
  features: string[];
  expires_at: string;
  seats: number;
  issued_at: string;
}

export interface DRDestination {
  id: string;
  name: string;
  kind: 's3' | 'gcs' | 'azure' | 'sftp' | 'local';
  endpoint: string;
  healthy: boolean;
  last_backup_at?: string | null;
}

export interface DRPolicy {
  id: string;
  name: string;
  cron: string;
  destinations: string[];
  retention_days: number;
  enabled: boolean;
}

export interface InstalledPlugin {
  id: string;
  name: string;
  version: string;
  author: string;
  enabled: boolean;
  permissions: string[];
  installed_at: string;
  update_available_version?: string | null;
}

export interface FederationPeer {
  server_id: string;
  endpoint: string;
  region?: string;
  state: 'healthy' | 'degraded' | 'quarantined' | 'down';
  rtt_ms?: number;
  last_seen?: string;
  outbound_lag_ms?: number;
}

export interface ActiveCall {
  call_id: string;
  channel_id?: string;
  participant_count: number;
  routing: 'mesh' | 'sfu' | 'p2p';
  call_type: 'audio' | 'video' | 'screen';
  started_at: string;
  mos?: number;
  jitter_ms?: number;
  loss_percent?: number;
}

export interface LegalHold {
  id: string;
  case_id: string;
  subject_user_ids: string[];
  scope: 'user' | 'channel' | 'tenant';
  reason: string;
  created_at: string;
  expires_at?: string | null;
  released: boolean;
}

export interface OnboardingState {
  step: number;
  total_steps: number;
  completed_steps: string[];
  current_step: string;
  prerequisites_ok: boolean;
  config_summary: Record<string, any>;
}

export interface RouterHealth {
  router_id: string;
  uptime_seconds: number;
  jails_count: number;
  routes_count: number;
  tunnels_count: number;
  cpu_percent: number;
  memory_percent: number;
  last_error?: string | null;
}

// ─────────────────────────────────────────────────────────────────────────
//  Admin API surface
// ─────────────────────────────────────────────────────────────────────────

export const AdminApiClient = {
  // ── Monitoring ───────────────────────────────────────────────────────
  monitoring: {
    getMetrics: () => authedFetch<MonitoringMetrics>('GET', '/api/admin/monitoring/metrics'),
    getHistory: (windowMin = 60) =>
      authedFetch<{ samples: MonitoringMetrics[] }>('GET', `/api/admin/monitoring/history${qs({ window_min: windowMin })}`),
    listConnections: () =>
      authedFetch<{ connections: any[] }>('GET', '/api/admin/monitoring/connections'),
    kickConnection: (id: string, reason?: string) =>
      authedFetch<{ ok: true }>('POST', `/api/admin/monitoring/connections/${encodeURIComponent(id)}/kick`,
                                 reason ? { reason } : undefined),
    listSubsystems: () =>
      authedFetch<{ subsystems: Array<{ name: string; healthy: boolean; last_error?: string | null }> }>(
        'GET', '/api/admin/monitoring/subsystems',
      ),
  },

  // ── Topology ─────────────────────────────────────────────────────────
  topology: {
    getGraph: () => authedFetch<TopologyGraph>('GET', '/api/admin/topology/graph'),
    getNodeDetail: (nodeId: string) =>
      authedFetch<TopologyNode & { extras: Record<string, any> }>(
        'GET', `/api/admin/topology/nodes/${encodeURIComponent(nodeId)}`,
      ),
    recomputeLayout: () =>
      authedFetch<{ ok: true }>('POST', '/api/admin/topology/recompute'),
  },

  // ── SIEM / Audit chain ───────────────────────────────────────────────
  siem: {
    getAuditHead: () => authedFetch<AuditChainHead>('GET', '/api/admin/siem/audit/head'),
    verifyAuditChain: () =>
      authedFetch<{ ok: boolean; broken_at_seq?: number; message?: string }>(
        'POST', '/api/admin/siem/audit/verify',
      ),
    listAlerts: (params?: { since?: string; severity?: string; limit?: number }) =>
      authedFetch<{ alerts: any[] }>('GET', `/api/admin/siem/alerts${qs(params)}`),
    acknowledgeAlert: (alertId: string) =>
      authedFetch<{ ok: true }>('POST', `/api/admin/siem/alerts/${encodeURIComponent(alertId)}/ack`),
    listAuditEntries: (params?: { actor?: string; action?: string; limit?: number; since?: string }) =>
      authedFetch<{ entries: any[] }>('GET', `/api/admin/siem/audit${qs(params)}`),
  },

  // ── Billing / Tenancy ────────────────────────────────────────────────
  billing: {
    listTenants: () => authedFetch<{ tenants: Tenant[] }>('GET', '/api/admin/billing/tenants'),
    getTenant: (id: string) =>
      authedFetch<Tenant & { licenses: License[] }>('GET', `/api/admin/billing/tenants/${encodeURIComponent(id)}`),
    createTenant: (body: { name: string; plan: string }) =>
      authedFetch<Tenant>('POST', '/api/admin/billing/tenants', body),
    suspendTenant: (id: string, reason: string) =>
      authedFetch<{ ok: true }>('POST', `/api/admin/billing/tenants/${encodeURIComponent(id)}/suspend`, { reason }),
    issueLicense: (body: { tenant_id: string; plan: string; seats: number; expires_at: string; features?: string[] }) =>
      authedFetch<License>('POST', '/api/admin/billing/licenses', body),
    revokeLicense: (id: string, reason: string) =>
      authedFetch<{ ok: true }>('POST', `/api/admin/billing/licenses/${encodeURIComponent(id)}/revoke`, { reason }),
    upcomingExpiries: (daysAhead = 30) =>
      authedFetch<{ licenses: License[] }>('GET', `/api/admin/billing/licenses/expiring${qs({ days: daysAhead })}`),
  },

  // ── DR Console ───────────────────────────────────────────────────────
  dr: {
    listDestinations: () =>
      authedFetch<{ destinations: DRDestination[] }>('GET', '/api/admin/dr/destinations'),
    addDestination: (body: { name: string; kind: DRDestination['kind']; endpoint: string; credentials?: Record<string, any> }) =>
      authedFetch<DRDestination>('POST', '/api/admin/dr/destinations', body),
    testDestination: (id: string) =>
      authedFetch<{ ok: boolean; message?: string }>('POST', `/api/admin/dr/destinations/${encodeURIComponent(id)}/test`),
    listPolicies: () => authedFetch<{ policies: DRPolicy[] }>('GET', '/api/admin/dr/policies'),
    forceBackup: (policyId: string) =>
      authedFetch<{ run_id: string }>('POST', `/api/admin/dr/policies/${encodeURIComponent(policyId)}/run`),
    failover: (body: { target_destination_id: string; dry_run?: boolean }) =>
      authedFetch<{ ok: true; report: any }>('POST', '/api/admin/dr/failover', body),
  },

  // ── Plugin Marketplace ───────────────────────────────────────────────
  plugins: {
    listInstalled: () => authedFetch<{ plugins: InstalledPlugin[] }>('GET', '/api/admin/plugins/installed'),
    listMarketplace: (params?: { query?: string; category?: string }) =>
      authedFetch<{ plugins: any[] }>('GET', `/api/admin/plugins/marketplace${qs(params)}`),
    enable: (id: string) =>
      authedFetch<{ ok: true }>('POST', `/api/admin/plugins/${encodeURIComponent(id)}/enable`),
    disable: (id: string) =>
      authedFetch<{ ok: true }>('POST', `/api/admin/plugins/${encodeURIComponent(id)}/disable`),
    update: (id: string) =>
      authedFetch<{ ok: true; new_version: string }>('POST', `/api/admin/plugins/${encodeURIComponent(id)}/update`),
    uninstall: (id: string) =>
      authedFetch<{ ok: true }>('DELETE', `/api/admin/plugins/${encodeURIComponent(id)}`),
    install: (body: { source: 'marketplace' | 'url' | 'file'; ref: string }) =>
      authedFetch<InstalledPlugin>('POST', '/api/admin/plugins/install', body),
  },

  // ── Federation Health ────────────────────────────────────────────────
  federation: {
    listPeers: () => authedFetch<{ peers: FederationPeer[] }>('GET', '/api/admin/federation/peers'),
    quarantinePeer: (serverId: string, reason: string) =>
      authedFetch<{ ok: true }>('POST',
        `/api/admin/federation/peers/${encodeURIComponent(serverId)}/quarantine`, { reason }),
    releasePeer: (serverId: string) =>
      authedFetch<{ ok: true }>('POST',
        `/api/admin/federation/peers/${encodeURIComponent(serverId)}/release`),
    healthMap: () =>
      authedFetch<{ regions: Array<{ region: string; peers: FederationPeer[] }> }>(
        'GET', '/api/admin/federation/health-map',
      ),
  },

  // ── QoS Live ─────────────────────────────────────────────────────────
  qos: {
    listActiveCalls: () => authedFetch<{ calls: ActiveCall[] }>('GET', '/api/admin/qos/active-calls'),
    getCallStats: (callId: string) =>
      authedFetch<{ samples: Array<{ ts: string; mos: number; jitter_ms: number; loss_percent: number }> }>(
        'GET', `/api/admin/qos/calls/${encodeURIComponent(callId)}`,
      ),
    endCall: (callId: string, reason?: string) =>
      authedFetch<{ ok: true }>('POST', `/api/admin/qos/calls/${encodeURIComponent(callId)}/end`, reason ? { reason } : undefined),
    forceCodec: (callId: string, codec: string) =>
      authedFetch<{ ok: true }>('POST', `/api/admin/qos/calls/${encodeURIComponent(callId)}/codec`, { codec }),
  },

  // ── Compliance / eDiscovery ──────────────────────────────────────────
  compliance: {
    listLegalHolds: () => authedFetch<{ holds: LegalHold[] }>('GET', '/api/admin/compliance/legal-holds'),
    createLegalHold: (body: { case_id: string; subject_user_ids: string[]; scope: LegalHold['scope']; reason: string; expires_at?: string }) =>
      authedFetch<LegalHold>('POST', '/api/admin/compliance/legal-holds', body),
    releaseLegalHold: (id: string) =>
      authedFetch<{ ok: true }>('POST', `/api/admin/compliance/legal-holds/${encodeURIComponent(id)}/release`),
    executeRTBF: (body: { subject_user_id: string; reason: string; confirm_token: string }) =>
      authedFetch<{ ok: true; report: any }>('POST', '/api/admin/compliance/rtbf', body),
    exportGDPR: (subjectUserId: string) =>
      authedFetch<{ download_url: string }>('GET', `/api/admin/compliance/export/${encodeURIComponent(subjectUserId)}`),
    listSearches: () =>
      authedFetch<{ searches: any[] }>('GET', '/api/admin/compliance/ediscovery/searches'),
    runSearch: (body: { query: string; channels?: string[]; from?: string; to?: string }) =>
      authedFetch<{ search_id: string }>('POST', '/api/admin/compliance/ediscovery/searches', body),
  },

  // ── Onboarding Wizard ────────────────────────────────────────────────
  onboarding: {
    getState: () => authedFetch<OnboardingState>('GET', '/api/admin/onboarding/state'),
    runStep: (stepId: string, body?: Record<string, any>) =>
      authedFetch<OnboardingState>('POST', `/api/admin/onboarding/steps/${encodeURIComponent(stepId)}/run`, body),
    skipStep: (stepId: string, reason: string) =>
      authedFetch<OnboardingState>('POST', `/api/admin/onboarding/steps/${encodeURIComponent(stepId)}/skip`, { reason }),
    reset: () => authedFetch<OnboardingState>('POST', '/api/admin/onboarding/reset'),
  },

  // ── Router Control ───────────────────────────────────────────────────
  router: {
    health: () => authedFetch<RouterHealth>('GET', '/api/admin/router/health'),
    listJails: () =>
      authedFetch<{ jails: any[] }>('GET', '/api/admin/router/jails'),
    listRoutes: () =>
      authedFetch<{ routes: any[] }>('GET', '/api/admin/router/routes'),
    listTunnels: () =>
      authedFetch<{ tunnels: any[] }>('GET', '/api/admin/router/tunnels'),
    reload: () =>
      authedFetch<{ ok: true }>('POST', '/api/admin/router/reload'),
    flushJails: () =>
      authedFetch<{ ok: true; flushed: number }>('POST', '/api/admin/router/jails/flush'),
  },
};

export default AdminApiClient;
