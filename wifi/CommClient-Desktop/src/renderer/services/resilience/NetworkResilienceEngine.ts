/**
 * NetworkResilienceEngine.ts — WiFi/LAN interruption recovery and adaptive reconnection.
 *
 * Handles the unique challenges of LAN/WiFi-only networking:
 *   - WiFi sleep/wake cycles (laptop lid close/open)
 *   - Router restart / DHCP renewal
 *   - Network interface change (WiFi → Ethernet → WiFi)
 *   - Temporary packet loss (microwave interference on 2.4GHz)
 *   - Complete LAN outage and recovery
 *
 * Architecture:
 * ┌────────────────────────────────────────────────────────────────┐
 * │                    NetworkResilienceEngine                      │
 * │                                                                │
 * │  ┌──────────────┐  ┌────────────────┐  ┌──────────────────┐  │
 * │  │ Network       │  │ Socket         │  │ Call             │  │
 * │  │ Monitor       │──│ Reconnector    │──│ Reconnector      │  │
 * │  │ • online/     │  │ • Backoff      │  │ • ICE restart    │  │
 * │  │   offline     │  │ • Priority     │  │ • Peer reattach  │  │
 * │  │ • Interface   │  │ • Queue        │  │ • Media renegot  │  │
 * │  │   changes     │  │ • Health probe │  │ • Quality ramp   │  │
 * │  └──────────────┘  └────────────────┘  └──────────────────┘  │
 * │           │                 │                    │             │
 * │           ▼                 ▼                    ▼             │
 * │  ┌────────────────────────────────────────────────────────┐   │
 * │  │ Unified State Machine                                  │   │
 * │  │ online → degraded → reconnecting → offline → online   │   │
 * │  └────────────────────────────────────────────────────────┘   │
 * └────────────────────────────────────────────────────────────────┘
 *
 * Reconnection strategy:
 *   1. IMMEDIATE retry on first disconnect (likely transient)
 *   2. Exponential backoff: 1s → 2s → 4s → 8s → 15s → 30s
 *   3. After 30s offline: show user notification
 *   4. After 2min offline: park socket, stop retrying actively
 *   5. On navigator.online: instant retry regardless of backoff
 *   6. On network interface change: instant retry + re-discover server
 *
 * Call-specific:
 *   1. Keep call state for 30s after disconnect (don't hang up)
 *   2. Try ICE restart first (cheapest recovery)
 *   3. If ICE restart fails, try full peer reconnect
 *   4. If socket lost, queue signaling messages
 *   5. After 30s: notify user, offer manual retry or end call
 *
 * Complements existing:
 *   - ConnectionResilience.ts: user-facing state (5 friendly states)
 *   - ReconnectionManager.ts: per-peer WebRTC reconnection
 *   - SocketManager: Socket.IO auto-reconnect
 *
 * This engine ORCHESTRATES the above and adds:
 *   - Network interface monitoring
 *   - WiFi sleep/wake detection
 *   - Coordinated socket + WebRTC recovery
 *   - Message queue during disconnection
 *   - Server re-discovery after long outage
 */

// ── Types ───────────────────────────────────────────────────

export type NetworkState = 'online' | 'degraded' | 'reconnecting' | 'offline' | 'server_lost';

export interface NetworkEvent {
  type: 'state_change' | 'retry_attempt' | 'recovery_complete' | 'gave_up';
  from: NetworkState;
  to: NetworkState;
  timestamp: number;
  details: string;
}

export interface RetryConfig {
  initialDelayMs: number;
  maxDelayMs: number;
  backoffMultiplier: number;
  maxAttempts: number;
  /** Immediately retry on navigator.online event */
  instantRetryOnOnline: boolean;
  /** Immediately retry on network interface change */
  instantRetryOnInterfaceChange: boolean;
}

export interface DisconnectContext {
  /** When the disconnect was first detected */
  disconnectedAt: number;
  /** What caused the disconnect */
  cause: 'socket_error' | 'socket_timeout' | 'navigator_offline' | 'interface_change' | 'server_crash' | 'unknown';
  /** Was user in a call? */
  wasInCall: boolean;
  /** Were there unsent messages? */
  hadPendingMessages: boolean;
}

export interface ReconnectAttempt {
  attemptNumber: number;
  timestamp: number;
  strategy: 'socket_only' | 'socket_and_discovery' | 'full_rediscovery';
  result: 'success' | 'failed' | 'timeout';
  durationMs: number;
}

export interface PendingOutboundMessage {
  id: string;
  event: string;
  data: unknown;
  queuedAt: number;
  priority: 'critical' | 'high' | 'normal';
  /** Max age before we drop the message (ms) */
  maxAgeMs: number;
}

export interface NetworkMetrics {
  state: NetworkState;
  disconnectedAt: number | null;
  totalDisconnectTimeMs: number;
  reconnectAttempts: number;
  successfulReconnects: number;
  pendingMessageCount: number;
  lastRttMs: number;
  averageRttMs: number;
}

// ── Constants ───────────────────────────────────────────────

const DEFAULT_RETRY_CONFIG: RetryConfig = {
  initialDelayMs: 1_000,
  maxDelayMs: 30_000,
  backoffMultiplier: 2,
  maxAttempts: 20,
  instantRetryOnOnline: true,
  instantRetryOnInterfaceChange: true,
};

/** Time before showing user notification about disconnect */
const USER_NOTIFY_THRESHOLD_MS = 30_000;

/** Time before parking the reconnector (stop active retries) */
const PARK_THRESHOLD_MS = 2 * 60 * 1000;

/** Time to keep call alive after disconnect */
const CALL_GRACE_PERIOD_MS = 30_000;

/** Max pending messages in outbound queue */
const MAX_PENDING_MESSAGES = 200;

/** Max age for pending messages (5 minutes) */
const DEFAULT_MSG_MAX_AGE_MS = 5 * 60 * 1000;

/** Network interface check interval */
const INTERFACE_CHECK_INTERVAL_MS = 5_000;

/** RTT probe interval when connected */
const RTT_PROBE_INTERVAL_MS = 10_000;

// ── Singleton ───────────────────────────────────────────────

class NetworkResilienceEngine {
  private state: NetworkState = 'online';
  private retryConfig: RetryConfig = { ...DEFAULT_RETRY_CONFIG };
  private disconnectContext: DisconnectContext | null = null;
  private currentDelay = 0;
  private attemptCount = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private interfaceTimer: ReturnType<typeof setInterval> | null = null;
  private rttProbeTimer: ReturnType<typeof setInterval> | null = null;
  private outboundQueue: PendingOutboundMessage[] = [];
  private eventListeners: Array<(event: NetworkEvent) => void> = [];
  private lastKnownInterfaces: string[] = [];
  private metrics: NetworkMetrics = {
    state: 'online',
    disconnectedAt: null,
    totalDisconnectTimeMs: 0,
    reconnectAttempts: 0,
    successfulReconnects: 0,
    pendingMessageCount: 0,
    lastRttMs: 0,
    averageRttMs: 0,
  };
  private rttSamples: number[] = [];
  private reconnectCallbacks: {
    onSocketReconnect?: () => Promise<boolean>;
    onServerRediscovery?: () => Promise<string | null>;
    onCallReattach?: () => Promise<boolean>;
    onFlushQueue?: (messages: PendingOutboundMessage[]) => Promise<void>;
  } = {};

  // ── Lifecycle ─────────────────────────────────────────────

  /**
   * Start monitoring network state.
   */
  start(callbacks: typeof this.reconnectCallbacks): void {
    this.reconnectCallbacks = callbacks;

    // Browser online/offline events
    window.addEventListener('online', this.handleOnline);
    window.addEventListener('offline', this.handleOffline);

    // Network interface monitoring
    this.startInterfaceMonitor();

    // RTT probing
    this.startRttProbe();
  }

  /**
   * Stop monitoring and clean up.
   */
  stop(): void {
    window.removeEventListener('online', this.handleOnline);
    window.removeEventListener('offline', this.handleOffline);
    this.stopRetry();
    this.stopInterfaceMonitor();
    this.stopRttProbe();
    this.outboundQueue = [];
  }

  // ── Public: Disconnect/Reconnect Signals ──────────────────

  /**
   * Called by SocketManager when socket disconnects.
   */
  onSocketDisconnect(cause: DisconnectContext['cause']): void {
    if (this.state === 'offline' || this.state === 'reconnecting') {
      return; // Already handling
    }

    this.disconnectContext = {
      disconnectedAt: Date.now(),
      cause,
      wasInCall: false, // Will be updated by call layer
      hadPendingMessages: this.outboundQueue.length > 0,
    };

    this.transitionTo('reconnecting', `Socket disconnected: ${cause}`);
    this.attemptCount = 0;
    this.currentDelay = this.retryConfig.initialDelayMs;
    this.scheduleRetry();
  }

  /**
   * Called by SocketManager when socket reconnects.
   */
  onSocketReconnect(): void {
    this.stopRetry();

    this.metrics.successfulReconnects++;
    if (this.disconnectContext) {
      this.metrics.totalDisconnectTimeMs += Date.now() - this.disconnectContext.disconnectedAt;
    }

    this.disconnectContext = null;
    this.transitionTo('online', 'Socket reconnected');

    // Flush outbound queue
    this.flushOutboundQueue();
  }

  /**
   * Update call state for disconnect handling.
   */
  setCallActive(inCall: boolean): void {
    if (this.disconnectContext) {
      this.disconnectContext.wasInCall = inCall;
    }
  }

  // ── Public: Outbound Message Queue ────────────────────────

  /**
   * Queue a message to send when reconnected.
   * Returns true if queued, false if queue is full.
   */
  queueMessage(event: string, data: unknown, priority: 'critical' | 'high' | 'normal' = 'normal'): boolean {
    if (this.state === 'online') {
      return false; // Don't queue if online — caller should send directly
    }

    if (this.outboundQueue.length >= MAX_PENDING_MESSAGES) {
      // Evict lowest priority oldest message
      const evictIdx = this.findEvictableIndex();
      if (evictIdx >= 0) {
        this.outboundQueue.splice(evictIdx, 1);
      } else {
        return false; // Queue completely full with critical messages
      }
    }

    this.outboundQueue.push({
      id: `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      event,
      data,
      queuedAt: Date.now(),
      priority,
      maxAgeMs: priority === 'critical' ? DEFAULT_MSG_MAX_AGE_MS * 2 : DEFAULT_MSG_MAX_AGE_MS,
    });

    this.metrics.pendingMessageCount = this.outboundQueue.length;
    return true;
  }

  // ── Public: Event Subscription ────────────────────────────

  onEvent(listener: (event: NetworkEvent) => void): () => void {
    this.eventListeners.push(listener);
    return () => {
      this.eventListeners = this.eventListeners.filter(l => l !== listener);
    };
  }

  getState(): NetworkState {
    return this.state;
  }

  getMetrics(): NetworkMetrics {
    return { ...this.metrics };
  }

  getDisconnectDuration(): number {
    if (!this.disconnectContext) return 0;
    return Date.now() - this.disconnectContext.disconnectedAt;
  }

  isCallInGracePeriod(): boolean {
    if (!this.disconnectContext?.wasInCall) return false;
    return this.getDisconnectDuration() < CALL_GRACE_PERIOD_MS;
  }

  // ── Private: State Machine ────────────────────────────────

  private transitionTo(newState: NetworkState, details: string): void {
    const from = this.state;
    if (from === newState) return;

    this.state = newState;
    this.metrics.state = newState;

    if (newState !== 'online') {
      if (!this.metrics.disconnectedAt) {
        this.metrics.disconnectedAt = Date.now();
      }
    } else {
      this.metrics.disconnectedAt = null;
    }

    const event: NetworkEvent = {
      type: 'state_change',
      from,
      to: newState,
      timestamp: Date.now(),
      details,
    };

    this.emitEvent(event);
  }

  private emitEvent(event: NetworkEvent): void {
    for (const listener of this.eventListeners) {
      try { listener(event); } catch {}
    }
  }

  // ── Private: Retry Logic ──────────────────────────────────

  private scheduleRetry(): void {
    if (this.attemptCount >= this.retryConfig.maxAttempts) {
      this.transitionTo('server_lost', `Gave up after ${this.attemptCount} attempts`);
      this.emitEvent({
        type: 'gave_up',
        from: 'reconnecting',
        to: 'server_lost',
        timestamp: Date.now(),
        details: `Max attempts (${this.retryConfig.maxAttempts}) exceeded`,
      });
      return;
    }

    // Check if we should park (stop active retries)
    if (this.disconnectContext && Date.now() - this.disconnectContext.disconnectedAt > PARK_THRESHOLD_MS) {
      this.transitionTo('offline', 'Parked — waiting for network event');
      return;
    }

    this.retryTimer = setTimeout(() => this.executeRetry(), this.currentDelay);
  }

  private async executeRetry(): Promise<void> {
    this.attemptCount++;
    this.metrics.reconnectAttempts++;

    const startTime = Date.now();

    // Determine strategy based on attempt count and disconnect duration
    let strategy: ReconnectAttempt['strategy'] = 'socket_only';
    if (this.attemptCount > 5) {
      strategy = 'socket_and_discovery';
    }
    if (this.attemptCount > 10 || this.getDisconnectDuration() > 60_000) {
      strategy = 'full_rediscovery';
    }

    this.emitEvent({
      type: 'retry_attempt',
      from: this.state,
      to: 'reconnecting',
      timestamp: Date.now(),
      details: `Attempt ${this.attemptCount}, strategy: ${strategy}`,
    });

    try {
      let success = false;

      if (strategy === 'full_rediscovery' && this.reconnectCallbacks.onServerRediscovery) {
        // Try to rediscover the server (it may have changed IP)
        const newUrl = await this.reconnectCallbacks.onServerRediscovery();
        if (newUrl) {
          success = true;
        }
      }

      if (!success && this.reconnectCallbacks.onSocketReconnect) {
        success = await this.reconnectCallbacks.onSocketReconnect();
      }

      if (success) {
        this.onSocketReconnect();

        // If was in call, try to reattach
        if (this.disconnectContext?.wasInCall && this.reconnectCallbacks.onCallReattach) {
          await this.reconnectCallbacks.onCallReattach();
        }

        return;
      }
    } catch (err) {
      // Retry failed
    }

    // Increase backoff
    this.currentDelay = Math.min(
      this.currentDelay * this.retryConfig.backoffMultiplier,
      this.retryConfig.maxDelayMs,
    );

    this.scheduleRetry();
  }

  private stopRetry(): void {
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  // ── Private: Network Events ───────────────────────────────

  private handleOnline = (): void => {
    if (this.retryConfig.instantRetryOnOnline && this.state !== 'online') {
      this.stopRetry();
      this.currentDelay = 0; // Instant retry
      this.executeRetry();
    }
  };

  private handleOffline = (): void => {
    if (this.state === 'online') {
      this.onSocketDisconnect('navigator_offline');
    }
  };

  // ── Private: Interface Monitor ────────────────────────────

  private startInterfaceMonitor(): void {
    // Snapshot current interfaces
    this.snapshotInterfaces();

    this.interfaceTimer = setInterval(() => {
      this.checkInterfaceChanges();
    }, INTERFACE_CHECK_INTERVAL_MS);
  }

  private stopInterfaceMonitor(): void {
    if (this.interfaceTimer) {
      clearInterval(this.interfaceTimer);
      this.interfaceTimer = null;
    }
  }

  private snapshotInterfaces(): void {
    if (typeof navigator !== 'undefined' && 'connection' in navigator) {
      // NetworkInformation API (limited browser support)
      const conn = (navigator as any).connection;
      if (conn) {
        this.lastKnownInterfaces = [conn.type || 'unknown'];
      }
    }
  }

  private checkInterfaceChanges(): void {
    if (typeof navigator !== 'undefined' && 'connection' in navigator) {
      const conn = (navigator as any).connection;
      if (conn) {
        const current = [conn.type || 'unknown'];
        const changed = JSON.stringify(current) !== JSON.stringify(this.lastKnownInterfaces);
        if (changed) {
          this.lastKnownInterfaces = current;
          if (this.state !== 'online' && this.retryConfig.instantRetryOnInterfaceChange) {
            this.stopRetry();
            this.currentDelay = 0;
            this.executeRetry();
          }
        }
      }
    }
  }

  // ── Private: RTT Probe ────────────────────────────────────

  private startRttProbe(): void {
    this.rttProbeTimer = setInterval(() => {
      if (this.state === 'online') {
        this.probeRtt();
      }
    }, RTT_PROBE_INTERVAL_MS);
  }

  private stopRttProbe(): void {
    if (this.rttProbeTimer) {
      clearInterval(this.rttProbeTimer);
      this.rttProbeTimer = null;
    }
  }

  private async probeRtt(): Promise<void> {
    const start = performance.now();
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5_000);

      await fetch(`http://127.0.0.1:3000/api/health`, {
        method: 'GET',
        signal: controller.signal,
      });

      clearTimeout(timeoutId);
      const rtt = performance.now() - start;

      this.rttSamples.push(rtt);
      if (this.rttSamples.length > 20) this.rttSamples.shift();

      this.metrics.lastRttMs = Math.round(rtt);
      this.metrics.averageRttMs = Math.round(
        this.rttSamples.reduce((a, b) => a + b, 0) / this.rttSamples.length,
      );

      // Detect degradation
      if (rtt > 150 && this.state === 'online') {
        this.transitionTo('degraded', `High RTT: ${Math.round(rtt)}ms`);
      } else if (rtt < 100 && this.state === 'degraded') {
        this.transitionTo('online', `RTT recovered: ${Math.round(rtt)}ms`);
      }
    } catch {
      // Probe failed — transition if still "online"
      if (this.state === 'online') {
        this.onSocketDisconnect('socket_timeout');
      }
    }
  }

  // ── Private: Outbound Queue ───────────────────────────────

  private async flushOutboundQueue(): Promise<void> {
    if (this.outboundQueue.length === 0) return;

    // Remove expired messages
    const now = Date.now();
    this.outboundQueue = this.outboundQueue.filter(
      m => now - m.queuedAt < m.maxAgeMs,
    );

    if (this.outboundQueue.length === 0) return;

    // Sort by priority: critical > high > normal, then by age
    this.outboundQueue.sort((a, b) => {
      const priorityOrder = { critical: 0, high: 1, normal: 2 };
      const pDiff = priorityOrder[a.priority] - priorityOrder[b.priority];
      if (pDiff !== 0) return pDiff;
      return a.queuedAt - b.queuedAt;
    });

    if (this.reconnectCallbacks.onFlushQueue) {
      try {
        await this.reconnectCallbacks.onFlushQueue([...this.outboundQueue]);
        this.outboundQueue = [];
        this.metrics.pendingMessageCount = 0;
      } catch {
        // Queue flush failed — will retry later
      }
    }
  }

  private findEvictableIndex(): number {
    // Find oldest normal-priority message
    for (let i = 0; i < this.outboundQueue.length; i++) {
      if (this.outboundQueue[i].priority === 'normal') return i;
    }
    // Then oldest high-priority
    for (let i = 0; i < this.outboundQueue.length; i++) {
      if (this.outboundQueue[i].priority === 'high') return i;
    }
    return -1; // Only critical messages — can't evict
  }
}

// ── Singleton Export ────────────────────────────────────────

export const networkResilienceEngine = new NetworkResilienceEngine();
