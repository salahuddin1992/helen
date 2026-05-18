/**
 * HTTP API client — wraps fetch with auth, retry, and error handling.
 * All REST calls to the CommClient backend go through here.
 */

import type { ProfilePhoto } from '../types';

let _baseUrl = 'http://127.0.0.1:3000';
let _accessToken: string | null = null;
let _refreshToken: string | null = null;
let _onTokenRefreshed: ((access: string, refresh: string) => void) | null = null;
let _onAuthFailed: (() => void) | null = null;

export function configureApi(opts: {
  baseUrl: string;
  accessToken?: string;
  refreshToken?: string;
  onTokenRefreshed?: (access: string, refresh: string) => void;
  onAuthFailed?: () => void;
}) {
  _baseUrl = opts.baseUrl.replace(/\/$/, '');
  _accessToken = opts.accessToken || null;
  _refreshToken = opts.refreshToken || null;
  _onTokenRefreshed = opts.onTokenRefreshed || null;
  _onAuthFailed = opts.onAuthFailed || null;
}

export function setTokens(access: string, refresh: string) {
  _accessToken = access;
  _refreshToken = refresh;
}

/**
 * Audit fix M1: expose the configured token-refresh callback so the
 * socket-level refresh path (`socket.manager.refreshAccessToken`)
 * can invoke it after rotating the access token, keeping auth.store
 * + tokenLifecycle in sync.
 */
export function getOnTokenRefreshed():
  | ((access: string, refresh: string) => void)
  | null {
  return _onTokenRefreshed;
}

export function getBaseUrl(): string {
  return _baseUrl;
}

// Single-flight guard so concurrent 401s from parallel requests (notably
// the resumable uploader's 4-way chunk fan-out) collapse onto one refresh
// round-trip instead of stampeding the auth endpoint.
let _refreshInFlight: Promise<boolean> | null = null;

async function refreshTokens(): Promise<boolean> {
  if (!_refreshToken) return false;
  if (_refreshInFlight) return _refreshInFlight;
  _refreshInFlight = (async () => {
    // Audit fix: refresh request had no AbortSignal/timeout. A
    // hung server (or a half-open TCP socket) would block the
    // single-flight guard forever — every subsequent 401-driven
    // refresh would see _refreshInFlight set and return the same
    // pending promise. The whole app stops receiving fresh auth.
    // 8s is enough for a slow LAN refresh; 401-driven retries are
    // already serialized.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 8_000);
    try {
      const res = await fetch(`${_baseUrl}/api/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: _refreshToken }),
        signal: controller.signal,
      });
      if (!res.ok) return false;
      const data = await res.json();
      _accessToken = data.access_token;
      _refreshToken = data.refresh_token;
      _onTokenRefreshed?.(data.access_token, data.refresh_token);
      return true;
    } catch {
      return false;
    } finally {
      clearTimeout(timeoutId);
      _refreshInFlight = null;
    }
  })();
  return _refreshInFlight;
}

/**
 * Public refresh helper — used by non-REST clients (e.g. ResumableUploader)
 * that need to recover their bearer token after a 401 from a long-running
 * request. Returns true if a new access token is now available.
 */
export async function refreshTokensIfPossible(): Promise<boolean> {
  return refreshTokens();
}

/** Current access token, or null if not logged in. */
export function getAccessToken(): string | null {
  return _accessToken;
}

/** Notify the app that auth has failed terminally (no refresh available). */
export function notifyAuthFailed(): void {
  try { _onAuthFailed?.(); } catch { /* ignore */ }
}

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
    this.name = 'ApiError';
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  retried = false,
  rateRetried = 0,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (_accessToken) headers['Authorization'] = `Bearer ${_accessToken}`;
  if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';

  const res = await fetch(`${_baseUrl}${path}`, {
    method,
    headers,
    body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401 && !retried) {
    const refreshed = await refreshTokens();
    if (refreshed) return request<T>(method, path, body, true, rateRetried);
    _onAuthFailed?.();
    throw new ApiError(401, 'Authentication failed');
  }

  // 429 rate-limit: honour Retry-After once before surfacing the error.
  // Server emits X-RateLimit-{Limit,Remaining,Reset,Class} so good
  // clients can pace; we just back off a single attempt for transient
  // bursts (chat send during a 5s spike, etc.). Two consecutive 429s
  // mean we genuinely exceeded the budget and the caller needs to know.
  if (res.status === 429 && rateRetried < 1) {
    const retryAfter = Number(res.headers.get('Retry-After') || '1');
    const waitMs = Math.min(5000, Math.max(250, retryAfter * 1000));
    await new Promise((r) => setTimeout(r, waitMs));
    return request<T>(method, path, body, retried, rateRetried + 1);
  }

  if (res.status === 204) return undefined as T;

  const data = await res.json();
  if (!res.ok) throw new ApiError(res.status, data.detail || data.error || 'Request failed');
  return data as T;
}

/**
 * Fetch a binary resource that requires auth and return a blob URL suitable
 * for <img src="...">. Caller is responsible for URL.revokeObjectURL when done.
 */
export async function fetchAuthorizedBlobUrl(path: string): Promise<string> {
  const headers: Record<string, string> = {};
  if (_accessToken) headers['Authorization'] = `Bearer ${_accessToken}`;
  let res = await fetch(`${_baseUrl}${path}`, { headers });
  if (res.status === 401) {
    const refreshed = await refreshTokens();
    if (refreshed) {
      headers['Authorization'] = `Bearer ${_accessToken}`;
      res = await fetch(`${_baseUrl}${path}`, { headers });
    }
  }
  if (!res.ok) throw new ApiError(res.status, `Failed to fetch image ${path}`);
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

// ── Typed API Methods ───────────────────────────────

export const api = {
  // Auth
  register: (body: { username: string; display_name: string; password: string }) =>
    request<any>('POST', '/api/auth/register', body),
  login: (body: { username: string; password: string; device_name?: string }) =>
    request<any>('POST', '/api/auth/login', body),
  logout: (refresh_token?: string) =>
    request<void>('POST', '/api/auth/logout', refresh_token ? { refresh_token } : undefined),
  changePassword: (current_password: string, new_password: string) =>
    request<void>('POST', '/api/auth/change-password', { current_password, new_password }),
  adminResetPassword: (target_user_id: string, new_password: string) =>
    request<void>('POST', `/api/admin/reset-password/${target_user_id}`, { new_password }),

  // Users
  getMe: () => request<any>('GET', '/api/users/me'),
  updateMe: (body: Record<string, any>) => request<any>('PATCH', '/api/users/me', body),
  /** Set or replace the user's custom status message. ``expires_at``
   *  is an ISO 8601 timestamp; ``null`` means "never expires". */
  setStatusMessage: (
    status_message: string,
    status_expires_at: string | null = null,
  ) =>
    request<any>('PUT', '/api/users/me/status-message', {
      status_message,
      status_expires_at,
    }),
  clearStatusMessage: () =>
    request<any>('DELETE', '/api/users/me/status-message'),
  listUsers: (params?: { search?: string; skip?: number; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.search) qs.set('search', params.search);
    if (params?.skip) qs.set('skip', String(params.skip));
    if (params?.limit) qs.set('limit', String(params.limit));
    return request<any>('GET', `/api/users?${qs.toString()}`);
  },
  getUser: (id: string) => request<any>('GET', `/api/users/${id}`),
  // Share-code lookup (local + federated fallback). Server returns the user
  // profile plus `origin_server` (null = local, { id, url, name } = remote).
  lookupByCode: (code: string) =>
    request<any>('GET', `/api/users/by-code/${encodeURIComponent(code)}`),

  // Server identity (its 64-char federation handle) + LAN peers. Used in
  // the header chip and search modal to show "you are connected to X, and
  // your search also spans N peer servers".
  getServerIdentity: () =>
    request<{
      server_code: string; server_id: string; name: string;
      host: string; port: number; uptime_seconds: number;
    }>('GET', '/api/peers/me'),
  listPeers: () =>
    request<{
      self: { server_id: string; name: string; host: string; port: number };
      peers: Array<{ server_id: string; name: string; host: string; port: number }>;
      total: number;
    }>('GET', '/api/peers'),

  // Profile photos
  listMyProfilePhotos: () =>
    request<{ photos: ProfilePhoto[]; total: number }>('GET', '/api/users/me/photos'),
  listUserProfilePhotos: (userId: string) =>
    request<{ photos: ProfilePhoto[]; total: number }>('GET', `/api/users/${userId}/photos`),
  uploadProfilePhoto: (
    file: File,
    opts?: { visibility?: 'public' | 'contacts' | 'private'; caption?: string; makePrimary?: boolean },
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('visibility', opts?.visibility || 'public');
    if (opts?.caption) form.append('caption', opts.caption);
    form.append('make_primary', opts?.makePrimary ? 'true' : 'false');
    return request<ProfilePhoto>('POST', '/api/users/me/photos', form);
  },
  updateProfilePhoto: (
    photoId: string,
    body: { visibility?: 'public' | 'contacts' | 'private'; is_primary?: boolean; caption?: string; position?: number },
  ) => request<ProfilePhoto>('PATCH', `/api/users/me/photos/${photoId}`, body),
  deleteProfilePhoto: (photoId: string) =>
    request<void>('DELETE', `/api/users/me/photos/${photoId}`),

  // Admin — runtime server configuration
  getServerConfig: () =>
    request<{ server_name: string }>('GET', '/api/admin/server-config'),
  updateServerName: (name: string) =>
    request<{ server_name: string }>('PATCH', '/api/admin/server-config', { server_name: name }),
  updateServerConfig: (body: Record<string, any>) =>
    request<any>('PATCH', '/api/admin/server-config', body),

  // Admin — comprehensive operator surface used by the AdminPanel inside the
  // Electron desktop. All require admin role server-side; UI is gated by
  // user.role === 'admin' before mounting the panel.
  admin: {
    // Dashboard
    stats: () => request<any>('GET', '/api/admin/stats'),
    activeCalls: () => request<{ calls: any[]; count: number }>('GET', '/api/admin/active-calls'),
    connectedClients: () => request<any>('GET', '/api/admin/connected-clients'),

    // Users
    listUsers: (params?: { search?: string; skip?: number; limit?: number }) => {
      const qs = new URLSearchParams();
      if (params?.search) qs.set('search', params.search);
      if (params?.skip != null) qs.set('skip', String(params.skip));
      if (params?.limit != null) qs.set('limit', String(params.limit));
      return request<any>('GET', `/api/users?${qs.toString()}`);
    },
    kick: (userId: string) =>
      request<any>('POST', `/api/admin/kick/${userId}`),
    ban: (userId: string, reason?: string) =>
      request<any>('POST', `/api/admin/ban/${userId}`, reason ? { reason } : undefined),
    unban: (userId: string) =>
      request<any>('POST', `/api/admin/unban/${userId}`),
    setRole: (userId: string, role: string) =>
      request<any>('POST', `/api/admin/set-role/${userId}`, { role }),
    resetPassword: (userId: string, new_password: string) =>
      request<void>('POST', `/api/admin/reset-password/${userId}`, { new_password }),
    getUserSessions: (userId: string) =>
      request<any>('GET', `/api/admin/users/${userId}/sessions`),
    revokeUserSession: (userId: string, sessionId: string) =>
      request<void>('DELETE', `/api/admin/users/${userId}/sessions/${sessionId}`),
    revokeAllUserSessions: (userId: string) =>
      request<any>('POST', `/api/admin/users/${userId}/sessions/revoke-all`),

    // Audit
    auditLogs: (params?: { limit?: number; event?: string; user_id?: string; success?: boolean }) => {
      const qs = new URLSearchParams();
      if (params?.limit != null) qs.set('limit', String(params.limit));
      if (params?.event) qs.set('event', params.event);
      if (params?.user_id) qs.set('user_id', params.user_id);
      if (params?.success != null) qs.set('success', String(params.success));
      return request<any>('GET', `/api/admin/audit-logs?${qs.toString()}`);
    },
    auditEvents: () => request<any>('GET', '/api/admin/audit-logs/events'),

    // DLQ
    dlqList: (params?: { limit?: number; status_filter?: string; kind_filter?: string }) => {
      const qs = new URLSearchParams();
      if (params?.limit != null) qs.set('limit', String(params.limit));
      if (params?.status_filter) qs.set('status_filter', params.status_filter);
      if (params?.kind_filter) qs.set('kind_filter', params.kind_filter);
      return request<any>('GET', `/api/admin/dlq?${qs.toString()}`);
    },
    dlqStats: () => request<any>('GET', '/api/admin/dlq/stats'),
    dlqReplay: (entryId: string) =>
      request<any>('POST', `/api/admin/dlq/${entryId}/replay`),

    // Backups
    backupsList: () => request<any>('GET', '/api/admin/backups'),
    backupsScheduler: () => request<any>('GET', '/api/admin/backups/scheduler'),
    backupRunNow: () => request<any>('POST', '/api/admin/backups/run-now'),
    backupCreate: (name?: string) =>
      request<any>('POST', '/api/admin/backups', name ? { name } : undefined),
    backupRestore: (name: string) =>
      request<any>('POST', `/api/admin/backups/${encodeURIComponent(name)}/restore`),
    backupVerify: (name: string) =>
      request<any>('POST', `/api/admin/backups/${encodeURIComponent(name)}/verify`),
    backupDelete: (name: string) =>
      request<void>('DELETE', `/api/admin/backups/${encodeURIComponent(name)}`),
    backupDownloadUrl: (name: string) =>
      `${_baseUrl}/api/admin/backups/${encodeURIComponent(name)}/download`,

    // Connectivity
    connectivity: () => request<any>('GET', '/api/admin/connectivity'),
    tunnelConfigure: (body: { ws_url: string; token: string; display_name?: string }) =>
      request<any>('POST', '/api/admin/connectivity/tunnel', body),
    tunnelDisable: () =>
      request<void>('DELETE', '/api/admin/connectivity/tunnel'),
    diagnosticsNetwork: () => request<any>('GET', '/api/admin/diagnostics/network'),

    // Federation
    federationStatus: () => request<any>('GET', '/api/admin/federation/status'),
    federationMetrics: () => request<any>('GET', '/api/admin/federation/metrics'),
    federationEvents: (limit?: number) =>
      request<any>('GET', `/api/admin/federation/events${limit != null ? `?limit=${limit}` : ''}`),
    federationBridges: () => request<any>('GET', '/api/admin/federation/bridges'),
    federationTopology: () => request<any>('GET', '/api/admin/federation/topology'),
    federationGenerateSecret: () =>
      request<any>('POST', '/api/admin/federation/generate-secret'),

    // Server config & roles
    serverConfig: () => request<any>('GET', '/api/admin/server-config'),
    updateServerConfig: (body: Record<string, any>) =>
      request<any>('PATCH', '/api/admin/server-config', body),
    serverRoles: () => request<any>('GET', '/api/admin/server-roles'),
    updateServerRoles: (body: Record<string, any>) =>
      request<any>('PATCH', '/api/admin/server-roles', body),

    // Control plane
    controlStatus: () => request<any>('GET', '/api/admin/control-plane/status'),
    controlDecisions: (limit?: number) =>
      request<any>('GET', `/api/admin/control-plane/decisions${limit != null ? `?limit=${limit}` : ''}`),
    controlSetProfile: (profile: string) =>
      request<any>('POST', '/api/admin/control-plane/profile', { profile }),
    controlEmergencyExit: () =>
      request<any>('POST', '/api/admin/control-plane/emergency/exit'),
    controlRooms: () => request<any>('GET', '/api/admin/control-plane/rooms'),

    // Placement
    placementNodes: () => request<any>('GET', '/api/admin/placement/nodes'),
    placementCapacity: () => request<any>('GET', '/api/admin/placement/capacity'),
    placementUpdateCapacity: (body: Record<string, any>) =>
      request<any>('PATCH', '/api/admin/placement/capacity', body),

    // Cleanup
    cleanupSessions: () => request<any>('POST', '/api/admin/cleanup/sessions'),
    cleanupFiles: () => request<any>('POST', '/api/admin/cleanup/files'),

    // SFU (mediasoup worker) — supervisor snapshot + live health probe.
    // Used by the admin dashboard to surface SFU readiness so operators
    // know whether large group calls (>4 mesh participants) will be
    // promoted to SFU or downgrade silently.
    sfuStatus: () => request<{
      enabled: boolean;
      running: boolean;
      healthy: boolean;
      pid: number | null;
      restart_count: number;
      last_exit_code: number | null;
      last_error: string | null;
      control_host: string;
      control_port: number;
      worker_root: string;
      stdout_log: string | null;
      stderr_log: string | null;
    }>('GET', '/api/admin/sfu/status'),
  },

  // Peer approval (admin role; lives under /api/admin/peers/*)
  adminPeers: {
    discovered: () => request<{ peers: any[] }>('GET', '/api/admin/peers/discovered'),
    pending: () => request<{ peers: any[] }>('GET', '/api/admin/peers/pending'),
    approved: () => request<{ peers: any[] }>('GET', '/api/admin/peers/approved'),
    rejected: () => request<{ peers: any[] }>('GET', '/api/admin/peers/rejected'),
    denied: () => request<{ peers: any[] }>('GET', '/api/admin/peers/denied'),
    approve: (serverId: string) =>
      request<any>('POST', `/api/admin/peers/${encodeURIComponent(serverId)}/approve`),
    reject: (serverId: string, reason: string) =>
      request<any>('POST', `/api/admin/peers/${encodeURIComponent(serverId)}/reject`, { reason }),
    deny: (serverId: string, reason: string) =>
      request<any>('POST', `/api/admin/peers/${encodeURIComponent(serverId)}/deny`, { reason }),
    ignore: (serverId: string) =>
      request<any>('POST', `/api/admin/peers/${encodeURIComponent(serverId)}/ignore`),
    trustPermanently: (serverId: string) =>
      request<any>('POST', `/api/admin/peers/${encodeURIComponent(serverId)}/trust-permanently`),
    trustOnce: (serverId: string) =>
      request<any>('POST', `/api/admin/peers/${encodeURIComponent(serverId)}/trust-once`),
  },

  // Contacts
  listContacts: () => request<any[]>('GET', '/api/users/me/contacts'),
  addContact: (body: { contact_id: string; nickname?: string }) =>
    request<any>('POST', '/api/users/me/contacts', body),
  removeContact: (contactId: string) =>
    request<void>('DELETE', `/api/users/me/contacts/${contactId}`),

  // Channels
  createChannel: (body: { type: string; name?: string; description?: string; member_ids: string[] }) =>
    request<any>('POST', '/api/channels', body),
  listChannels: () => request<any>('GET', '/api/channels'),
  getChannel: (id: string) => request<any>('GET', `/api/channels/${id}`),
  updateChannel: (id: string, body: Record<string, any>) =>
    request<any>('PATCH', `/api/channels/${id}`, body),
  deleteChannel: (id: string) =>
    request<void>('DELETE', `/api/channels/${id}`),
  addMember: (channelId: string, body: { user_id: string; role?: string }) =>
    request<any>('POST', `/api/channels/${channelId}/members`, body),
  removeMember: (channelId: string, userId: string) =>
    request<void>('DELETE', `/api/channels/${channelId}/members/${userId}`),

  // Messages
  getMessages: (channelId: string, params?: { before?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.before) qs.set('before', params.before);
    if (params?.limit) qs.set('limit', String(params.limit));
    return request<any>('GET', `/api/channels/${channelId}/messages?${qs.toString()}`);
  },
  sendMessage: (channelId: string, body: { content: string; type?: string; reply_to?: string }) =>
    request<any>('POST', `/api/channels/${channelId}/messages`, body),
  searchMessages: (
    q: string,
    channelIdOrFilters?: string | {
      channel_id?: string;
      sender_id?: string;
      sender_username?: string;
      msg_type?: 'text' | 'file' | 'image' | 'reply' | 'system';
      has_file?: boolean;
      has_reactions?: boolean;
      is_pinned?: boolean;
      // ISO 8601 date strings — the server parses them as datetimes.
      date_from?: string;
      date_to?: string;
      limit?: number;
      offset?: number;
    },
  ) => {
    const qs = new URLSearchParams({ q });
    // Backwards-compatible: callers that pass a bare channel ID still work.
    if (typeof channelIdOrFilters === 'string') {
      if (channelIdOrFilters) qs.set('channel_id', channelIdOrFilters);
    } else if (channelIdOrFilters) {
      const f = channelIdOrFilters;
      if (f.channel_id) qs.set('channel_id', f.channel_id);
      if (f.sender_id) qs.set('sender_id', f.sender_id);
      if (f.sender_username) qs.set('sender_username', f.sender_username);
      if (f.msg_type) qs.set('msg_type', f.msg_type);
      if (f.has_file != null) qs.set('has_file', String(f.has_file));
      if (f.has_reactions != null) qs.set('has_reactions', String(f.has_reactions));
      if (f.is_pinned != null) qs.set('is_pinned', String(f.is_pinned));
      if (f.date_from) qs.set('date_from', f.date_from);
      if (f.date_to) qs.set('date_to', f.date_to);
      if (f.limit != null) qs.set('limit', String(f.limit));
      if (f.offset != null) qs.set('offset', String(f.offset));
    }
    return request<any>('GET', `/api/messages/search?${qs.toString()}`);
  },
  editMessage: (messageId: string, content: string) =>
    request<any>('PATCH', `/api/messages/${messageId}`, { content }),
  deleteMessage: (messageId: string) =>
    request<void>('DELETE', `/api/messages/${messageId}`),
  toggleReaction: (messageId: string, emoji: string) =>
    request<any>('POST', `/api/messages/${messageId}/reactions`, { emoji }),
  getMessageReceipts: (messageId: string) =>
    request<any>('GET', `/api/messages/${messageId}/receipts`),
  pinMessage: (messageId: string) =>
    request<any>('POST', `/api/messages/${messageId}/pin`),
  unpinMessage: (messageId: string) =>
    request<any>('DELETE', `/api/messages/${messageId}/pin`),
  getPinnedMessages: (channelId: string) =>
    request<any>('GET', `/api/channels/${channelId}/pins`),
  forwardMessage: (messageId: string, toChannelId: string) =>
    request<any>('POST', `/api/messages/${messageId}/forward`, { to_channel_id: toChannelId }),
  getMessageThread: (messageId: string, limit?: number, before?: string) => {
    const qs = new URLSearchParams();
    if (limit) qs.set('limit', String(limit));
    if (before) qs.set('before', before);
    return request<any>('GET', `/api/messages/${messageId}/thread?${qs.toString()}`);
  },

  // Channels - read state
  getChannelUnread: (channelId: string) =>
    request<any>('GET', `/api/channels/${channelId}/unread`),
  getChannelReadStates: (channelId: string) =>
    request<any>('GET', `/api/channels/${channelId}/read-states`),

  // Files
  uploadFile: (file: File, channelId?: string) => {
    const fd = new FormData();
    fd.append('file', file);
    const qs = channelId ? `?channel_id=${channelId}` : '';
    return request<any>('POST', `/api/files/upload${qs}`, fd);
  },
  getFileUrl: (fileId: string) => `${_baseUrl}/api/files/${fileId}`,
  getThumbnailUrl: (fileId: string) => `${_baseUrl}/api/files/${fileId}/thumbnail`,

  // Sessions
  listSessions: () => request<any>('GET', '/api/sessions'),
  revokeSession: (sessionId: string) => request<void>('DELETE', `/api/sessions/${sessionId}`),
  revokeAllSessions: () => request<any>('POST', '/api/sessions/revoke-all'),

  // Calls
  getCallHistory: () => request<any>('GET', '/api/calls'),
  deleteCall:        (id: string) => request<void>('DELETE', `/api/calls/${id}`),
  clearCallHistory:  ()           => request<void>('DELETE', '/api/calls'),

  // Active group call discovery — drives the "Join Existing Call" UX.
  // Backend: app/api/routes/calls.py::get_channel_active_call.
  // Returns { active_call: null } when no live call, otherwise the
  // call summary including the participant list snapshot.
  getChannelActiveCall: (channelId: string) =>
    request<{
      active_call: null | {
        call_id: string;
        call_type: 'audio' | 'video';
        routing: 'p2p' | 'mesh' | 'sfu' | 'hybrid';
        status: 'ringing' | 'active';
        started_at: string | null;
        participant_count: number;
        participants: Array<{
          user_id: string;
          muted: boolean;
          video_off: boolean;
          sharing_screen: boolean;
          on_hold: boolean;
        }>;
        host_id: string;
      };
    }>('GET', `/api/channels/${encodeURIComponent(channelId)}/active-call`),

  // Saved messages — bookmark + organize messages into folders.
  // Backend: app/api/routes/saved_messages.py.
  savedMessages: {
    list: (params?: { folder?: string; limit?: number; offset?: number }) => {
      const qs = new URLSearchParams();
      if (params?.folder) qs.set('folder', params.folder);
      if (params?.limit != null) qs.set('limit', String(params.limit));
      if (params?.offset != null) qs.set('offset', String(params.offset));
      return request<{ items: any[]; total: number }>('GET', `/api/saved?${qs.toString()}`);
    },
    folders: () => request<{ folders: string[] }>('GET', '/api/saved/folders'),
    save: (body: { message_id: string; folder?: string | null; note?: string | null }) =>
      request<any>('POST', '/api/saved', body),
    update: (messageId: string, body: { folder?: string | null; note?: string | null }) =>
      request<any>('PATCH', `/api/saved/${encodeURIComponent(messageId)}`, body),
    remove: (messageId: string) =>
      request<void>('DELETE', `/api/saved/${encodeURIComponent(messageId)}`),
  },

  // Notifications
  getNotifications: (params?: { limit?: number; offset?: number; unread_only?: boolean }) => {
    const qs = new URLSearchParams();
    if (params?.limit) qs.set('limit', String(params.limit));
    if (params?.offset) qs.set('offset', String(params.offset));
    if (params?.unread_only) qs.set('unread_only', 'true');
    return request<any>('GET', `/api/notifications?${qs.toString()}`);
  },
  getUnreadCount: () => request<any>('GET', '/api/notifications/count'),
  markNotificationsRead: (notification_ids: string[]) =>
    request<any>('POST', '/api/notifications/mark-read', { notification_ids }),
  markAllNotificationsRead: () =>
    request<any>('POST', '/api/notifications/mark-all-read'),
  deleteNotification: (id: string) =>
    request<void>('DELETE', `/api/notifications/${id}`),
  deleteAllNotifications: () =>
    request<any>('POST', '/api/notifications/delete-all'),

  // Group-file multicast (BitTorrent-style swarm for group channels)
  groupFileOffers: {
    create: (
      channelId: string,
      body: {
        file_id: string;
        chunk_size: number;
        total_chunks: number;
        caption?: string | null;
        swarm_enabled?: boolean;
        expires_in_sec?: number | null;
        checksum?: string | null;
      },
    ) =>
      request<any>('POST', `/api/channels/${channelId}/group-file-offers`, body),
    listChannel: (
      channelId: string,
      params?: { status?: string; limit?: number; offset?: number },
    ) => {
      const qs = new URLSearchParams();
      if (params?.status) qs.set('status', params.status);
      if (params?.limit) qs.set('limit', String(params.limit));
      if (params?.offset) qs.set('offset', String(params.offset));
      return request<any>(
        'GET',
        `/api/channels/${channelId}/group-file-offers?${qs.toString()}`,
      );
    },
    inbox: (activeOnly: boolean = true, limit: number = 50) => {
      const qs = new URLSearchParams({
        active_only: activeOnly ? 'true' : 'false',
        limit: String(limit),
      });
      return request<any>('GET', `/api/group-file-offers/inbox?${qs.toString()}`);
    },
    get: (offerId: string) =>
      request<any>('GET', `/api/group-file-offers/${offerId}`),
    stats: (offerId: string) =>
      request<any>('GET', `/api/group-file-offers/${offerId}/stats`),
    accept: (offerId: string) =>
      request<any>('POST', `/api/group-file-offers/${offerId}/accept`),
    reject: (offerId: string) =>
      request<any>('POST', `/api/group-file-offers/${offerId}/reject`),
    reportChunk: (offerId: string, chunkIndex: number, chunkBytes?: number) =>
      request<any>(
        'POST',
        `/api/group-file-offers/${offerId}/chunks/${chunkIndex}`,
        chunkBytes != null ? { chunk_bytes: chunkBytes } : {},
      ),
    chunkPeers: (offerId: string, chunkIndex: number, limit: number = 32) =>
      request<any>(
        'GET',
        `/api/group-file-offers/${offerId}/chunks/${chunkIndex}/peers?limit=${limit}`,
      ),
    cancel: (offerId: string) =>
      request<void>('DELETE', `/api/group-file-offers/${offerId}`),
  },

  // System
  health: () => request<any>('GET', '/api/health'),
  uplink: () => request<{
    connected: boolean;
    configured: boolean;
    method: string | null;
    ws_url: string | null;
    public_id: string | null;
    active_methods: string[];
  }>('GET', '/api/uplink'),

  // ICE configuration with short-lived TURN credentials. Server-side
  // endpoint at app/api/routes/turn.py — returns iceServers ready for
  // RTCPeerConnection. Cached client-side via iceConfigService.
  iceConfig: () => request<{
    ice_servers: RTCIceServer[];
    ice_transport_policy: 'all' | 'relay';
    ttl_seconds: number;
    realm: string;
  }>('GET', '/api/turn/ice-config'),
  info: () => request<any>('GET', '/api/info'),

  // Media policy (user-facing)
  getMyMediaCap: () => request<{
    cap: {
      max_width: number;
      max_height: number;
      max_framerate: number;
      max_bitrate_kbps: number;
      allow_8k: boolean;
      allow_client_override: boolean;
      enforce_hard_cap: boolean;
      auto_max_quality: boolean;
      source: string;
    };
    ladder: Array<{
      id: string;
      label: string;
      w: number;
      h: number;
      fps: number;
      kbps: number;
      requires_8k?: boolean;
    }>;
  }>('GET', '/api/media-policy/me'),

  // Media policy (admin)
  adminGetMediaPolicy: () =>
    request<any>('GET', '/api/admin/media-policy'),
  adminUpdateMediaPolicy: (body: Record<string, any>) =>
    request<any>('PATCH', '/api/admin/media-policy', body),
  adminListOverrides: () =>
    request<{ overrides: any[] }>('GET', '/api/admin/media-policy/overrides'),
  adminSetOverride: (userId: string, body: Record<string, any>) =>
    request<any>('PUT', `/api/admin/media-policy/overrides/${userId}`, body),
  adminClearOverride: (userId: string) =>
    request<void>('DELETE', `/api/admin/media-policy/overrides/${userId}`),

  // Ingest sources (admin)
  ingestCapabilities: () =>
    request<any>('GET', '/api/ingest/capabilities'),
  adminListIngestSources: () =>
    request<{ sources: any[] }>('GET', '/api/admin/ingest/sources'),
  adminCreateIngestSource: (body: Record<string, any>) =>
    request<any>('POST', '/api/admin/ingest/sources', body),
  adminGetIngestSource: (id: string) =>
    request<any>('GET', `/api/admin/ingest/sources/${id}`),
  adminUpdateIngestSource: (id: string, body: Record<string, any>) =>
    request<any>('PATCH', `/api/admin/ingest/sources/${id}`, body),
  adminDeleteIngestSource: (id: string) =>
    request<void>('DELETE', `/api/admin/ingest/sources/${id}`),
  adminStartIngestSource: (id: string) =>
    request<any>('POST', `/api/admin/ingest/sources/${id}/start`),
  adminStopIngestSource: (id: string) =>
    request<any>('POST', `/api/admin/ingest/sources/${id}/stop`),
  adminRestartIngestSource: (id: string) =>
    request<any>('POST', `/api/admin/ingest/sources/${id}/restart`),
  adminIngestSourceStatus: (id: string) =>
    request<any>('GET', `/api/admin/ingest/sources/${id}/status`),

  // Phone pairing — desktop asks for a short-lived token to QR-code.
  // The phone opens /pair?t=<token> on the server and claims it via Safari.
  requestPairToken: () =>
    request<{ pair_token: string; expires_in: number; pair_url_path: string }>(
      'POST',
      '/api/pair/request',
    ),

  /** List this user's currently-live phone pair sessions. */
  listPairSessions: () =>
    request<{
      sessions: Array<{
        phone_sid: string;
        user_id: string;
        label: string;
        user_agent: string;
        started_at: number;
        duration_s: number;
        claimed_by: string | null;
        transport: 'usb_tether' | 'wifi';
      }>;
    }>('GET', '/api/pair/sessions'),

  /** Force-disconnect a paired phone (e.g. lost device). */
  terminatePairSession: (phoneSid: string) =>
    request<void>('DELETE', `/api/pair/sessions/${encodeURIComponent(phoneSid)}`),

  // ── Calendar — internal events + ICS feed ─────────────────────────
  calendar: {
    list: (params?: { start?: number; end?: number; limit?: number }) => {
      const qs = new URLSearchParams();
      if (params?.start) qs.set('start', String(params.start));
      if (params?.end) qs.set('end', String(params.end));
      if (params?.limit) qs.set('limit', String(params.limit));
      return request<{ events: any[] }>('GET', `/api/calendar/events?${qs.toString()}`);
    },
    get: (id: string) => request<any>('GET', `/api/calendar/events/${encodeURIComponent(id)}`),
    create: (body: {
      title: string;
      start_at: number;
      end_at: number;
      description?: string;
      location?: string;
      channel_id?: string | null;
      attendees?: string[];
      recurrence?: string | null;
      reminders?: number[];
    }) => request<any>('POST', '/api/calendar/events', body),
    update: (id: string, body: Record<string, any>) =>
      request<any>('PATCH', `/api/calendar/events/${encodeURIComponent(id)}`, body),
    cancel: (id: string) =>
      request<void>('DELETE', `/api/calendar/events/${encodeURIComponent(id)}`),
    icsFeedUrl: () => '/api/calendar/feed.ics',
  },

  // ── Admin diagnostics: crash log + audit chain ────────────────────
  adminCrashes: {
    list: (params?: { limit?: number; level?: string }) => {
      const qs = new URLSearchParams();
      if (params?.limit) qs.set('limit', String(params.limit));
      if (params?.level) qs.set('level', params.level);
      return request<{ events: any[]; installed: boolean; count?: number }>(
        'GET', `/api/admin/crashes?${qs.toString()}`,
      );
    },
    get: (id: string) => request<any>('GET', `/api/admin/crashes/${encodeURIComponent(id)}`),
    purgeOlderThan: (days: number) =>
      request<{ deleted: number; days: number }>(
        'DELETE', `/api/admin/crashes/older-than/${days}`,
      ),
  },
  adminAuditChain: {
    head: () => request<any>('GET', '/api/admin/audit-chain/head'),
    verify: () => request<{ ok: boolean; broken_at_seq?: number; message: string }>(
      'POST', '/api/admin/audit-chain/verify',
    ),
    entries: (params?: { actor?: string; action?: string; since?: number; limit?: number }) => {
      const qs = new URLSearchParams();
      if (params?.actor) qs.set('actor', params.actor);
      if (params?.action) qs.set('action', params.action);
      if (params?.since) qs.set('since', String(params.since));
      if (params?.limit) qs.set('limit', String(params.limit));
      return request<{ entries: any[]; count: number }>(
        'GET', `/api/admin/audit-chain/entries?${qs.toString()}`,
      );
    },
  },

  // ── Transport backends — visibility into NATS / MQTT / gRPC / WG ─
  adminTransports: {
    summary: () => request<{
      broker_backend: string;
      federation_backend: string;
      vpn_backend: string;
      mesh_topology: string;
      active: { nats: boolean; mqtt: boolean; grpc_federation: boolean; wireguard: boolean };
    }>('GET', '/api/admin/transports/backends'),
    nats: () => request<any>('GET', '/api/admin/transports/nats/status'),
    mqtt: () => request<any>('GET', '/api/admin/transports/mqtt/status'),
    grpc: () => request<any>('GET', '/api/admin/transports/grpc/status'),
    wireguard: () => request<any>('GET', '/api/admin/transports/wireguard/status'),
  },

  // Invite / share / guest-auth codes — wraps /api/me/codes.
  // ``kind`` is one of "invite" / "guest_auth" / "share". Channel
  // invite links pass kind="invite" + target_channel_id.
  codes: {
    create: (body: {
      kind?: 'invite' | 'guest_auth' | 'share';
      note?: string;
      max_uses?: number | null;
      ttl_sec?: number | null;
      target_channel_id?: string | null;
    }) =>
      request<{
        code: string;
        kind: string;
        note: string;
        max_uses: number | null;
        uses_count: number;
        ttl_sec: number | null;
        expires_at: string | null;
        target_channel_id: string | null;
        created_at: string;
      }>('POST', '/api/me/codes', body),
    list: () =>
      request<{ codes: any[] }>('GET', '/api/me/codes'),
    revoke: (code: string) =>
      request<void>(
        'DELETE', `/api/me/codes/${encodeURIComponent(code)}`,
      ),
    redeem: (code: string) =>
      request<any>('POST', '/api/codes/redeem', { code }),
    /** Join a channel by redeeming an invite code in one step.
     *  Returns the resolved channel ID + name on success. */
    joinChannelByCode: (code: string) =>
      request<{
        ok: boolean;
        channel_id: string;
        channel_name: string;
        channel_type: string;
        already_member: boolean;
      }>('POST', '/api/channels/join-by-code', { code }),
  },

  // Custom emoji — admin-uploadable shortcodes. Listing is public
  // for any authed user; upload/delete are admin-only and the
  // server enforces the role.
  customEmoji: {
    list: () =>
      request<{
        emoji: Array<{
          id: string; shortcode: string; mime: string;
          size_bytes: number; uploaded_at: number;
          description: string; url: string;
        }>;
      }>('GET', '/api/custom-emoji'),
    rawUrl: (id: string) =>
      `${getBaseUrl()}/api/custom-emoji/${encodeURIComponent(id)}/raw`,
    upload: async (shortcode: string, file: File, description = '') => {
      // Multipart — bypass the JSON request helper.
      const fd = new FormData();
      fd.append('shortcode', shortcode);
      fd.append('description', description);
      fd.append('file', file);
      const token = getAccessToken();
      const res = await fetch(`${getBaseUrl()}/api/custom-emoji`, {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: fd,
      });
      const text = await res.text();
      const data = (() => {
        try { return JSON.parse(text); } catch { return text; }
      })();
      if (!res.ok) {
        throw new ApiError(
          res.status,
          (data && (data as any).detail) ||
          `upload failed (${res.status})`,
        );
      }
      return data;
    },
    delete: (id: string) =>
      request<void>(
        'DELETE',
        `/api/custom-emoji/${encodeURIComponent(id)}`,
      ),
  },

  // Scheduled messages — server already implements queue + delivery
  // worker. We wrap the four CRUD endpoints. ``send_at`` is an ISO
  // 8601 timestamp; the server's worker dispatches when the wall
  // clock crosses it.
  scheduledMessages: {
    create: (body: {
      channel_id: string;
      content: string;
      send_at: string;
      msg_type?: 'text' | 'file' | 'image' | 'reply' | 'system';
      reply_to?: string | null;
      file_id?: string | null;
    }) =>
      request<{
        id: string;
        channel_id: string;
        content: string;
        send_at: string;
        status: string;
      }>('POST', '/api/scheduled-messages', body),
    list: (channelId?: string) => {
      const qs = new URLSearchParams();
      if (channelId) qs.set('channel_id', channelId);
      return request<{ scheduled: any[] }>(
        'GET',
        `/api/scheduled-messages?${qs.toString()}`,
      );
    },
    update: (id: string, body: {
      content?: string; send_at?: string;
    }) =>
      request<any>(
        'PATCH',
        `/api/scheduled-messages/${encodeURIComponent(id)}`,
        body,
      ),
    cancel: (id: string) =>
      request<void>(
        'DELETE',
        `/api/scheduled-messages/${encodeURIComponent(id)}`,
      ),
  },

  // Per-member channel preferences — pin to top, archive, etc.
  // The server stores these on ``channel_members`` (already in
  // schema; see ``ChannelMember.is_pinned`` / ``is_archived``).
  channelPrefs: {
    pin: (channelId: string, pinned: boolean) =>
      request<{ is_pinned: boolean; is_archived: boolean }>(
        'PUT',
        `/api/channels/${encodeURIComponent(channelId)}/pin`,
        { pinned },
      ),
    archive: (channelId: string, archived: boolean) =>
      request<{ is_pinned: boolean; is_archived: boolean }>(
        'PUT',
        `/api/channels/${encodeURIComponent(channelId)}/archive`,
        { archived },
      ),
  },

  // Per-channel auto-delete (TTL). Cap is in seconds; 0 = off.
  // Server clamps to [60s, 30 days] when non-zero.
  channelTTL: {
    get: (channelId: string) =>
      request<{ channel_id: string; ttl_seconds: number }>(
        'GET',
        `/api/channels/${encodeURIComponent(channelId)}/ttl`,
      ),
    set: (channelId: string, seconds: number) =>
      request<{ channel_id: string; ttl_seconds: number }>(
        'PUT',
        `/api/channels/${encodeURIComponent(channelId)}/ttl`,
        { ttl_seconds: seconds },
      ),
    clear: (channelId: string) =>
      request<void>(
        'DELETE',
        `/api/channels/${encodeURIComponent(channelId)}/ttl`,
      ),
    sweepNow: (channelId: string) =>
      request<{ ok: boolean; channels: number; deleted: number }>(
        'POST',
        `/api/channels/${encodeURIComponent(channelId)}/ttl/sweep-now`,
      ),
  },

  // Per-channel slow-mode (admins set, members read).
  channelSlowMode: {
    get: (channelId: string) =>
      request<{
        channel_id: string;
        seconds_per_message: number;
      }>(
        'GET',
        `/api/channels/${encodeURIComponent(channelId)}/slow-mode`,
      ),
    set: (channelId: string, seconds: number) =>
      request<{
        channel_id: string;
        seconds_per_message: number;
      }>(
        'PUT',
        `/api/channels/${encodeURIComponent(channelId)}/slow-mode`,
        { seconds_per_message: seconds },
      ),
    clear: (channelId: string) =>
      request<void>(
        'DELETE',
        `/api/channels/${encodeURIComponent(channelId)}/slow-mode`,
      ),
  },

  // Online-Mode master gate. Status is readable by every authenticated
  // user (so the title-bar pill can render); enable/disable are
  // admin-only on the server, the UI only surfaces the buttons to
  // admins as a hint.
  onlineMode: {
    status: () =>
      request<{
        configured: boolean;
        enabled: boolean;
        last_change_at?: number | null;
        services?: Array<{ name: string; running: boolean }>;
      }>('GET', '/api/online-mode/status'),
    enable: (reason?: string | null) =>
      request<any>('POST', '/api/admin/online-mode/enable', {
        reason: reason ?? null,
      }),
    disable: (reason?: string | null) =>
      request<any>('POST', '/api/admin/online-mode/disable', {
        reason: reason ?? null,
      }),
  },
};
