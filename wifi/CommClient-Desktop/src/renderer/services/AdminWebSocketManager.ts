/**
 * AdminWebSocketManager — single multiplexed WS manager for the 7 admin
 * live-update channels:
 *   - metrics       /api/admin/ws/metrics
 *   - topology      /api/admin/ws/topology
 *   - audit         /api/admin/ws/audit
 *   - dr            /api/admin/ws/dr
 *   - plugins       /api/admin/ws/plugins
 *   - federation    /api/admin/ws/federation
 *   - qos           /api/admin/ws/qos
 *
 * Each channel is an independent ws:// connection backed by:
 *   - per-channel pub/sub (subscribe/unsubscribe with handler list)
 *   - exponential-backoff auto-reconnect (capped at 30s)
 *   - token refresh on 4401 close codes
 *   - opportunistic re-subscribe on reconnect
 *
 * Why N sockets instead of one multiplexed bus?
 * ──────────────────────────────────────────────
 * Backpressure isolation. Topology graphs and QoS sample streams can each
 * burst at >100 msg/s; routing them over a single socket means one channel
 * can starve another. The server already exposes them as independent WS
 * endpoints — we mirror that on the client.
 *
 * Why not socket.io? The admin streams are unidirectional broadcasts with
 * occasional control frames; raw WS keeps payloads compact and avoids the
 * extra socket.io handshake.
 */

import { getBaseUrl, getAccessToken, refreshTokensIfPossible } from './api.client';

export type AdminWsChannel =
  | 'metrics'
  | 'topology'
  | 'audit'
  | 'dr'
  | 'plugins'
  | 'federation'
  | 'qos';

export interface AdminWsMessage<T = unknown> {
  channel: AdminWsChannel;
  type: string;          // e.g. 'metric.sample', 'topology.snapshot', 'audit.alert'
  ts: number;            // unix ms
  data: T;
}

export type AdminWsHandler = (msg: AdminWsMessage) => void;

export interface AdminWsManagerOptions {
  /** Initial reconnect delay in ms. Default: 500. */
  initialBackoffMs?: number;
  /** Maximum reconnect delay in ms. Default: 30000. */
  maxBackoffMs?: number;
  /** Number of consecutive auth failures before we give up. Default: 3. */
  maxAuthFailures?: number;
  /** Heartbeat (ping) interval in ms. 0 to disable. Default: 25000. */
  heartbeatMs?: number;
}

interface ChannelState {
  channel: AdminWsChannel;
  ws: WebSocket | null;
  handlers: Set<AdminWsHandler>;
  backoffMs: number;
  authFailures: number;
  pendingReconnect: ReturnType<typeof setTimeout> | null;
  heartbeatTimer: ReturnType<typeof setInterval> | null;
  closed: boolean;
}

const DEFAULT_OPTS: Required<AdminWsManagerOptions> = {
  initialBackoffMs: 500,
  maxBackoffMs: 30_000,
  maxAuthFailures: 3,
  heartbeatMs: 25_000,
};

// All 7 channels we expose.
const ALL_CHANNELS: AdminWsChannel[] = [
  'metrics', 'topology', 'audit', 'dr', 'plugins', 'federation', 'qos',
];

export class AdminWebSocketManager {
  private states: Map<AdminWsChannel, ChannelState> = new Map();
  private opts: Required<AdminWsManagerOptions>;
  private stopped = false;

  constructor(opts: AdminWsManagerOptions = {}) {
    this.opts = { ...DEFAULT_OPTS, ...opts };
    for (const ch of ALL_CHANNELS) {
      this.states.set(ch, {
        channel: ch,
        ws: null,
        handlers: new Set(),
        backoffMs: this.opts.initialBackoffMs,
        authFailures: 0,
        pendingReconnect: null,
        heartbeatTimer: null,
        closed: false,
      });
    }
  }

  /** Subscribe to a channel. The connection is opened lazily on first
   *  subscription; closed automatically when the last subscriber leaves. */
  subscribe(channel: AdminWsChannel, handler: AdminWsHandler): () => void {
    const st = this.states.get(channel);
    if (!st) throw new Error(`Unknown admin WS channel: ${channel}`);
    st.handlers.add(handler);
    if (!st.ws) this.openSocket(st);
    return () => this.unsubscribe(channel, handler);
  }

  unsubscribe(channel: AdminWsChannel, handler: AdminWsHandler): void {
    const st = this.states.get(channel);
    if (!st) return;
    st.handlers.delete(handler);
    if (st.handlers.size === 0) this.closeSocket(st);
  }

  /** Globally close every channel. Use on logout. */
  shutdown(): void {
    this.stopped = true;
    for (const st of this.states.values()) {
      st.handlers.clear();
      this.closeSocket(st);
    }
  }

  /** Re-arm every active socket — useful when the access token rotates so
   *  channels that disconnected on 4401 get re-opened immediately. */
  reconnectAll(): void {
    if (this.stopped) return;
    for (const st of this.states.values()) {
      if (st.handlers.size > 0) {
        this.closeSocket(st, /* keepHandlers */ true);
        this.openSocket(st);
      }
    }
  }

  /** Snapshot of which channels are open — used by AdminNotifications
   *  to show a green/red indicator next to the bell. */
  status(): Record<AdminWsChannel, 'open' | 'connecting' | 'closed'> {
    const out = {} as Record<AdminWsChannel, 'open' | 'connecting' | 'closed'>;
    for (const [k, v] of this.states.entries()) {
      out[k] = v.ws
        ? (v.ws.readyState === WebSocket.OPEN ? 'open' : 'connecting')
        : 'closed';
    }
    return out;
  }

  // ── Internal ────────────────────────────────────────────────────────
  private openSocket(st: ChannelState): void {
    if (this.stopped) return;
    if (st.pendingReconnect) {
      clearTimeout(st.pendingReconnect);
      st.pendingReconnect = null;
    }

    const baseHttp = getBaseUrl();
    if (!baseHttp) return;
    const wsBase = baseHttp.replace(/^http/i, 'ws').replace(/\/+$/, '');
    const token = getAccessToken();
    // Bearer is forwarded via the Sec-WebSocket-Protocol subprotocol header
    // because browsers don't allow custom WS headers. The server reads the
    // protocol entry that starts with "bearer." (same convention as the
    // existing socket.io path).
    const protocols = token ? [`bearer.${token}`, 'helen-admin.v1'] : ['helen-admin.v1'];
    let ws: WebSocket;
    try {
      ws = new WebSocket(`${wsBase}/api/admin/ws/${st.channel}`, protocols);
    } catch (e) {
      // Construction failed (invalid URL). Schedule a backoff retry.
      this.scheduleReconnect(st);
      return;
    }
    st.ws = ws;

    ws.onopen = () => {
      st.backoffMs = this.opts.initialBackoffMs;
      st.authFailures = 0;
      st.closed = false;
      // Heartbeat
      if (this.opts.heartbeatMs > 0) {
        st.heartbeatTimer = setInterval(() => {
          try {
            if (ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'ping', ts: Date.now() }));
            }
          } catch { /* ignore */ }
        }, this.opts.heartbeatMs);
      }
    };

    ws.onmessage = (ev) => {
      let parsed: AdminWsMessage | null = null;
      try {
        parsed = JSON.parse(ev.data) as AdminWsMessage;
      } catch {
        return;
      }
      if (!parsed || parsed.type === 'pong') return;
      // Ensure the channel tag matches what we expected — the server may
      // omit it for compactness.
      if (!parsed.channel) parsed.channel = st.channel;
      for (const h of st.handlers) {
        try { h(parsed); }
        catch (err) {
          // Swallow handler errors so one bad subscriber can't break the
          // channel for everyone else.

          console.error(`[AdminWS:${st.channel}] handler error:`, err);
        }
      }
    };

    ws.onerror = () => {
      // Just log; onclose will run the reconnect logic.

      console.warn(`[AdminWS:${st.channel}] socket error`);
    };

    ws.onclose = async (ev) => {
      this.clearHeartbeat(st);
      st.ws = null;
      if (st.closed || this.stopped) return;

      // 4401 → auth-failed close code (server signals invalid token).
      if (ev.code === 4401) {
        st.authFailures += 1;
        if (st.authFailures >= this.opts.maxAuthFailures) {

          console.error(`[AdminWS:${st.channel}] gave up after ${st.authFailures} auth failures`);
          return;
        }
        // Attempt a token refresh before retry.
        const ok = await refreshTokensIfPossible();
        if (!ok) {

          console.error(`[AdminWS:${st.channel}] token refresh failed`);
          return;
        }
      }

      this.scheduleReconnect(st);
    };
  }

  private scheduleReconnect(st: ChannelState): void {
    if (this.stopped || st.handlers.size === 0) return;
    const delay = st.backoffMs;
    st.backoffMs = Math.min(this.opts.maxBackoffMs, Math.max(500, st.backoffMs * 2));
    st.pendingReconnect = setTimeout(() => {
      st.pendingReconnect = null;
      this.openSocket(st);
    }, delay);
  }

  private closeSocket(st: ChannelState, keepHandlers = false): void {
    st.closed = true;
    if (!keepHandlers) st.handlers.clear();
    this.clearHeartbeat(st);
    if (st.pendingReconnect) {
      clearTimeout(st.pendingReconnect);
      st.pendingReconnect = null;
    }
    if (st.ws) {
      try { st.ws.close(1000, 'client_shutdown'); } catch { /* ignore */ }
      st.ws = null;
    }
  }

  private clearHeartbeat(st: ChannelState): void {
    if (st.heartbeatTimer) {
      clearInterval(st.heartbeatTimer);
      st.heartbeatTimer = null;
    }
  }
}

// Singleton — keeps the admin streams persistent across panel switches.
// The AdminPanel mounts/unmounts as the user navigates, but we keep the
// notification stream alive globally so the bell icon never misses an event.
let _singleton: AdminWebSocketManager | null = null;
export function getAdminWsManager(): AdminWebSocketManager {
  if (!_singleton) _singleton = new AdminWebSocketManager();
  return _singleton;
}

/** Tear down on logout. */
export function destroyAdminWsManager(): void {
  if (_singleton) {
    _singleton.shutdown();
    _singleton = null;
  }
}
