/**
 * Socket.IO client manager — connects to the backend, emits/listens for all real-time events.
 * Single connection shared across the entire app.
 */
import { io, Socket } from 'socket.io-client';
import { AppLogger } from './AppLogger';

const log = AppLogger.create('Socket');

type EventCallback = (...args: any[]) => void;

class SocketManager {
  private socket: Socket | null = null;
  private url: string = '';
  private token: string = '';
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 50;
  private heartbeatInterval: ReturnType<typeof setInterval> | null = null;
  private eventListeners: Map<string, Set<EventCallback>> = new Map();
  private connectionCallbacks: {
    onConnect?: () => void;
    onDisconnect?: (reason: string) => void;
    onReconnectFailed?: () => void;
  } = {};

  connect(url: string, token: string, callbacks?: typeof this.connectionCallbacks): void {
    this.url = url;
    this.token = token;
    this.connectionCallbacks = callbacks || {};

    if (this.socket?.connected) this.socket.disconnect();

    // Detect Helen-Rendezvous tunnel URLs of the form
    //   http(s)://host[:port]/t/<public_id>
    // Socket.IO hard-codes its engine path to /socket.io/ and ignores any
    // path component in the URL, so we must split the origin and set
    // `path` explicitly so the WS upgrade hits the tunnel's WS proxy.
    //
    // Audit fix M2: previous regex `\/t\/[^/]+` accepted any character
    // up to the next slash, including spaces, semicolons, query
    // strings, and arbitrary length — meaning a malicious URL could
    // craft a public_id that injects auth-leaking redirects. Restrict
    // to alphanumeric + dash/underscore, capped at 128 chars (the
    // tunnel server issues UUIDs of ~32 chars; 128 is safety margin).
    const tunnelMatch = url.match(
      /^(https?:\/\/[^/]+)(\/t\/[A-Za-z0-9_-]{1,128})\/?$/i,
    );
    const origin = tunnelMatch ? tunnelMatch[1] : url;
    const path = tunnelMatch ? `${tunnelMatch[2]}/socket.io/` : '/socket.io/';

    this.socket = io(origin, {
      path,
      auth: { token },
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionAttempts: this.maxReconnectAttempts,
      // Audit fix: deterministic delay made every client reconnect
      // at the same offset after a server restart, producing a
      // thundering-herd reconnect storm. randomizationFactor 0.5
      // spreads the second-N reconnect over [0.5N..1.5N] seconds.
      reconnectionDelay: 1000,
      reconnectionDelayMax: 10000,
      randomizationFactor: 0.5,
      timeout: 10000,
    });

    this.socket.on('connect', () => {
      log.info('Connected', { sid: this.socket?.id });
      this.reconnectAttempts = 0;
      this.startHeartbeat();
      // Re-register event listeners (socket.io already handles reconnect, but ensure clean state)
      this.connectionCallbacks.onConnect?.();
    });

    this.socket.on('disconnect', (reason) => {
      log.info('Disconnected', { reason });
      this.stopHeartbeat();
      this.connectionCallbacks.onDisconnect?.(reason);
    });

    this.socket.on('connect_error', (err) => {
      this.reconnectAttempts++;
      log.warn('Connect error', { error: err.message, attempt: this.reconnectAttempts });
      if (this.reconnectAttempts >= this.maxReconnectAttempts) {
        this.stopHeartbeat();
        this.connectionCallbacks.onReconnectFailed?.();
      }
    });

    // Calendar reminders → re-broadcast as a window CustomEvent so
    // CalendarPage + the OS notification surface can react without
    // having to know about socket.io.
    this.socket.on('calendar:reminder', (payload: any) => {
      try {
        window.dispatchEvent(new CustomEvent('calendar:reminder', {
          detail: payload,
        }));
      } catch (e) {
        log.debug('calendar_reminder_dispatch_failed', { error: String(e) });
      }
    });

    // Re-register all stored listeners
    for (const [event, callbacks] of this.eventListeners) {
      for (const cb of callbacks) {
        this.socket.on(event, cb);
      }
    }
  }

  disconnect(): void {
    this.stopHeartbeat();
    this.socket?.disconnect();
    this.socket = null;
  }

  updateToken(token: string): void {
    this.token = token;
    if (this.socket) {
      (this.socket.auth as any).token = token;
    }
  }

  /**
   * Mid-session token refresh. Issues `auth:refresh` over the live
   * socket; on success, the new access token is applied to:
   *   1. the socket's own `auth.token` (for any future reconnects)
   *   2. the API client (so subsequent HTTP calls use the new token)
   *   3. localStorage (persists across renderer restarts)
   *
   * Returns the new access_token on success, null on failure (the
   * caller should fall back to a full re-login).
   */
  async refreshAccessToken(refreshToken: string): Promise<string | null> {
    if (!this.socket?.connected) return null;
    try {
      const resp = await this.emit('auth_refresh', { refresh_token: refreshToken }, 8_000);
      if (!resp || !resp.ok || !resp.access_token) return null;

      // Audit fix M1: propagate the refreshed access token to BOTH
      // the live socket auth AND the api.client + auth.store via the
      // existing onTokenRefreshed callback. Previously we only
      // updated this socket's `auth.token`; api.client kept the OLD
      // access token, so HTTP calls made between socket-refresh and
      // the next HTTP-401 still carried the expired bearer.
      this.updateToken(resp.access_token);
      try {
        // Lazy import to avoid circular dep:
        // socket.manager → api.client → fetch wrapper that imports
        // tokenLifecycle which imports socket.manager.
        const { setTokens, getOnTokenRefreshed } = await import('./api.client');
        // Refresh-token may rotate too — server is free to issue a new
        // one. Honor the response if it carries `refresh_token`.
        const newRefresh = (resp as any).refresh_token || refreshToken;
        setTokens(resp.access_token, newRefresh);
        const cb = getOnTokenRefreshed?.();
        if (cb) {
          await cb(resp.access_token, newRefresh);
        }
      } catch (e) {
        log.warn('auth_refresh callback propagation failed', { error: (e as Error).message });
      }

      return resp.access_token as string;
    } catch (e) {
      log.warn('auth_refresh failed', { error: (e as Error).message });
      return null;
    }
  }

  isConnected(): boolean {
    return this.socket?.connected ?? false;
  }

  // ── Event Listening ────────────────────────────────

  on(event: string, callback: EventCallback): () => void {
    if (!this.eventListeners.has(event)) {
      this.eventListeners.set(event, new Set());
    }
    this.eventListeners.get(event)!.add(callback);
    this.socket?.on(event, callback);

    // Return unsubscribe function
    return () => {
      this.eventListeners.get(event)?.delete(callback);
      this.socket?.off(event, callback);
    };
  }

  off(event: string, callback: EventCallback): void {
    this.eventListeners.get(event)?.delete(callback);
    this.socket?.off(event, callback);
  }

  // ── Event Emitting ─────────────────────────────────

  emit(event: string, data?: any, timeoutMs = 10_000): Promise<any> {
    return new Promise((resolve, reject) => {
      if (!this.socket?.connected) {
        reject(new Error('Socket not connected'));
        return;
      }

      // Timeout guard to prevent forever-hanging promises
      const timer = setTimeout(() => {
        reject(new Error(`Socket emit '${event}' timed out after ${timeoutMs}ms`));
      }, timeoutMs);

      this.socket.emit(event, data, (response: any) => {
        clearTimeout(timer);
        if (response?.error) reject(new Error(response.error));
        else resolve(response);
      });
    });
  }

  emitNoAck(event: string, data?: any): void {
    if (!this.socket?.connected) {
      log.warn('emitNoAck skipped — not connected', { event });
      return;
    }
    this.socket.emit(event, data);
  }

  // ── Heartbeat ──────────────────────────────────────

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatInterval = setInterval(() => {
      this.emitNoAck('presence_heartbeat', {});
    }, 5000);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
  }
}

// Singleton
export const socketManager = new SocketManager();

// ── Trace observation ──────────────────────────────────────────────
// Every incoming socket event is inspected for a trace_id/event_id
// pair. Untraced legacy events skip — zero overhead. Traced events
// land in the in-memory ring buffer (`traceReporter.getRecent()`)
// plus a console line in dev mode so operators can grep IDs straight
// from DevTools. The observer never intercepts payloads — handlers
// run unchanged.
//
// We install on the singleton at module load. The check guarantees
// idempotence if a hot-reload re-imports the module.
import { installTraceObserver } from './network/TraceReporter';

let _observerInstalled = false;
if (!_observerInstalled) {
  try {
    installTraceObserver(socketManager);
    _observerInstalled = true;
  } catch (e) {
    // Never let tracing setup break the socket layer.
     
    log.warn('traceObserver install failed', { error: (e as Error)?.message });
  }
}
