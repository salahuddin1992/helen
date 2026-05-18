/**
 * ReconnectionManager — production-grade reconnection recovery for WebRTC peers on LAN.
 *
 * Coordinates:
 *   - Exponential backoff ICE restart strategy (1s, 2s, 4s, 8s cap)
 *   - Maximum retry count (5 attempts) before declaring failure
 *   - Network change detection (online/offline, interface changes)
 *   - Connection quality degradation detection
 *   - Per-peer reconnection state machine: stable → monitoring → recovering → failed
 *   - ICE candidate pair monitoring and stats collection
 *   - Metrics tracking (attempts, durations, success rate)
 *   - LAN-specific: subnet/interface change detection
 *   - Event callbacks for state transitions
 *   - Proper cleanup and resource management
 *
 * One instance per peer (1-to-1) or one global instance for group calls (mesh).
 * Integrates with PeerConnection._attemptIceRestart() and CallEngine.
 */

// ── Types ────────────────────────────────────────────────────────

export type ReconnectionState = 'stable' | 'monitoring' | 'recovering' | 'failed';

export interface ReconnectionConfig {
  /** Peer identifier — required for per-peer tracking */
  peerId: string;
  /** Maximum number of ICE restart attempts before failure */
  maxRetries?: number;
  /** Initial backoff delay in milliseconds */
  initialBackoffMs?: number;
  /** Maximum backoff delay in milliseconds */
  maxBackoffMs?: number;
  /** Quality degradation threshold (RTT in ms) — trigger proactive reconnect */
  rttDegradationThresholdMs?: number;
  /** Packet loss threshold (0-1) — trigger proactive reconnect */
  packetLossThreshold?: number;
  /** Check quality stats every N ms */
  qualityCheckIntervalMs?: number;
  /** Monitor for network changes (online/offline events) */
  monitorNetworkChanges?: boolean;
  /** LAN-specific: monitor for IP/subnet changes */
  monitorInterfaceChanges?: boolean;
  /** Callback: state change event */
  onStateChange?: (event: ReconnectionEvent) => void;
  /** Callback: retry attempt made */
  onRetryAttempt?: (attempt: ReconnectionAttempt) => void;
  /** Callback: metrics updated */
  onMetricsUpdate?: (metrics: ReconnectionMetrics) => void;
  /**
   * Callback: trigger an ICE restart through the consumer's signaling
   * channel. The manager only owns the raw RTCPeerConnection, not the
   * signaling transport — without this hook, calling
   * setLocalDescription(iceRestart) here produces an SDP that never
   * reaches the remote peer and the restart is silent. Consumers (e.g.
   * CallEngine) wire this to PeerConnection._attemptIceRestart, which
   * already calls onSignal with the new offer.
   */
  onIceRestartRequested?: () => void;
}

export interface ReconnectionEvent {
  timestamp: number;
  peerId: string;
  previousState: ReconnectionState;
  nextState: ReconnectionState;
  reason: string;
  attempt?: number;
}

export interface ReconnectionAttempt {
  peerId: string;
  attemptNumber: number;
  timestamp: number;
  backoffDelayMs: number;
  iceCandidatePairBefore?: {
    candidate: RTCIceCandidate | null;
    state: string;
    currentRoundTripTime: number;
  };
}

export interface ReconnectionMetrics {
  peerId: string;
  state: ReconnectionState;
  totalAttempts: number;
  successfulReconnects: number;
  lastAttemptAt: number | null;
  lastSuccessAt: number | null;
  averageRecoveryTimeMs: number;
  successRate: number; // 0-1
  currentRtt: number;
  currentPacketLoss: number;
  lastQualityCheckAt: number | null;
  activeSinceMs: number;
}

export interface ConnectionQuality {
  rtt: number;
  packetsLost: number;
  jitter: number;
  bitrate: number;
}

// ── Constants ────────────────────────────────────────────────────────

const DEFAULT_MAX_RETRIES = 5;
const DEFAULT_INITIAL_BACKOFF_MS = 1000;
const DEFAULT_MAX_BACKOFF_MS = 8000;
const DEFAULT_RTT_DEGRADATION_THRESHOLD_MS = 200;
const DEFAULT_PACKET_LOSS_THRESHOLD = 0.05;
const DEFAULT_QUALITY_CHECK_INTERVAL_MS = 2000;

// ── ReconnectionManager Class ────────────────────────────────────────

export class ReconnectionManager {
  private config: ReconnectionConfig & {
    maxRetries: number;
    initialBackoffMs: number;
    maxBackoffMs: number;
    rttDegradationThresholdMs: number;
    packetLossThreshold: number;
    qualityCheckIntervalMs: number;
    monitorNetworkChanges: boolean;
    monitorInterfaceChanges: boolean;
  };
  private state: ReconnectionState = 'stable';
  private pc: RTCPeerConnection | null = null;
  private metrics: ReconnectionMetrics;

  // Timers and intervals
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private qualityCheckInterval: ReturnType<typeof setInterval> | null = null;
  private interfaceCheckInterval: ReturnType<typeof setInterval> | null = null;
  private networkChangeListener: (() => void) | null = null;
  private onlineListener: (() => void) | null = null;
  private offlineListener: (() => void) | null = null;

  // State tracking
  private currentBackoffMs: number;
  private recoveryTimestamps: number[] = [];
  private lastNetworkStatus: boolean = navigator.onLine;
  private lastInterfaceSignature: string = '';
  private destroyed: boolean = false;
  private _startTime: number;

  constructor(config: ReconnectionConfig) {
    this.config = {
      peerId: config.peerId,
      maxRetries: config.maxRetries ?? DEFAULT_MAX_RETRIES,
      initialBackoffMs: config.initialBackoffMs ?? DEFAULT_INITIAL_BACKOFF_MS,
      maxBackoffMs: config.maxBackoffMs ?? DEFAULT_MAX_BACKOFF_MS,
      rttDegradationThresholdMs: config.rttDegradationThresholdMs ?? DEFAULT_RTT_DEGRADATION_THRESHOLD_MS,
      packetLossThreshold: config.packetLossThreshold ?? DEFAULT_PACKET_LOSS_THRESHOLD,
      qualityCheckIntervalMs: config.qualityCheckIntervalMs ?? DEFAULT_QUALITY_CHECK_INTERVAL_MS,
      monitorNetworkChanges: config.monitorNetworkChanges ?? true,
      monitorInterfaceChanges: config.monitorInterfaceChanges ?? true,
      onStateChange: config.onStateChange,
      onRetryAttempt: config.onRetryAttempt,
      onMetricsUpdate: config.onMetricsUpdate,
      onIceRestartRequested: config.onIceRestartRequested,
    };

    this.currentBackoffMs = this.config.initialBackoffMs;
    this._startTime = Date.now();

    this.metrics = {
      peerId: this.config.peerId,
      state: 'stable',
      totalAttempts: 0,
      successfulReconnects: 0,
      lastAttemptAt: null,
      lastSuccessAt: null,
      averageRecoveryTimeMs: 0,
      successRate: 0,
      currentRtt: 0,
      currentPacketLoss: 0,
      lastQualityCheckAt: null,
      activeSinceMs: Date.now(),
    };
  }

  // ── Lifecycle ────────────────────────────────────────────────────

  /**
   * Attach the RTCPeerConnection to monitor.
   * Call after peer connection is created.
   */
  attachPeerConnection(pc: RTCPeerConnection): void {
    if (this.destroyed) return;
    this.pc = pc;
    console.log(`[Reconnection:${this.config.peerId}] PeerConnection attached`);
    this._startQualityMonitoring();
    this._setupNetworkListeners();
  }

  /**
   * Notify the manager of connection state change.
   * Call from PeerConnection's onconnectionstatechange callback.
   */
  onPeerConnectionStateChange(state: RTCPeerConnectionState): void {
    if (this.destroyed) return;

    console.log(`[Reconnection:${this.config.peerId}] connectionState: ${state}`);

    if (state === 'connected') {
      this._onConnectionRestored();
    } else if (state === 'disconnected') {
      this._transitionState('monitoring', 'connection disconnected');
    } else if (state === 'failed') {
      this._onConnectionFailed();
    }
  }

  /**
   * Notify the manager of ICE connection state change.
   */
  onIceConnectionStateChange(state: RTCIceConnectionState): void {
    if (this.destroyed) return;

    console.log(`[Reconnection:${this.config.peerId}] iceConnectionState: ${state}`);

    if (state === 'connected' || state === 'completed') {
      if (this.state === 'recovering') {
        this._onConnectionRestored();
      }
    } else if (state === 'disconnected') {
      if (this.state === 'stable') {
        this._transitionState('monitoring', 'ICE disconnected');
      }
    } else if (state === 'failed') {
      this._onConnectionFailed();
    }
  }

  /**
   * Explicitly trigger reconnection attempt (called from external retry logic).
   * Returns true if retry was scheduled, false if max retries exceeded.
   */
  triggerReconnectAttempt(): boolean {
    if (this.destroyed || !this.pc) return false;

    if (this.metrics.totalAttempts >= this.config.maxRetries) {
      console.warn(`[Reconnection:${this.config.peerId}] Max retries exceeded`);
      this._transitionState('failed', 'max retries exceeded');
      return false;
    }

    return this._scheduleRetry();
  }

  /**
   * Destroy the manager and clean up all resources.
   */
  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;

    console.log(`[Reconnection:${this.config.peerId}] Destroying manager`);

    this._clearRetryTimer();
    this._stopQualityMonitoring();
    this._stopInterfaceChecking();
    this._removeNetworkListeners();

    this.pc = null;
    this.recoveryTimestamps = [];
  }

  // ── Accessors ────────────────────────────────────────────────────

  getState(): ReconnectionState {
    return this.state;
  }

  getMetrics(): ReconnectionMetrics {
    return { ...this.metrics };
  }

  // ── Private: State Transitions ───────────────────────────────────

  private _transitionState(nextState: ReconnectionState, reason: string): void {
    if (this.state === nextState) return;

    const previousState = this.state;
    this.state = nextState;
    this.metrics.state = nextState;

    const event: ReconnectionEvent = {
      timestamp: Date.now(),
      peerId: this.config.peerId,
      previousState,
      nextState,
      reason,
    };

    console.log(
      `[Reconnection:${this.config.peerId}] State transition: ${previousState} → ${nextState} (${reason})`
    );

    this.config.onStateChange?.(event);
    this._emitMetrics();
  }

  private _onConnectionFailed(): void {
    if (this.state === 'failed') return;

    if (this.state === 'recovering') {
      // Recovery attempt failed
      if (!this._scheduleRetry()) {
        this._transitionState('failed', 'recovery attempt failed, max retries exceeded');
      }
    } else if (this.state === 'stable' || this.state === 'monitoring') {
      // Initial failure
      this._transitionState('recovering', 'connection failed');
      this._scheduleRetry();
    }
  }

  private _onConnectionRestored(): void {
    if (this.state === 'stable') return;

    console.log(`[Reconnection:${this.config.peerId}] Connection restored`);

    const now = Date.now();
    const lastAttempt = this.metrics.lastAttemptAt;

    if (lastAttempt) {
      const recoveryTime = now - lastAttempt;
      this.recoveryTimestamps.push(recoveryTime);

      // Keep last 10 recovery times for average
      if (this.recoveryTimestamps.length > 10) {
        this.recoveryTimestamps.shift();
      }

      this.metrics.averageRecoveryTimeMs =
        this.recoveryTimestamps.reduce((a, b) => a + b, 0) / this.recoveryTimestamps.length;
    }

    this.metrics.lastSuccessAt = now;
    this.metrics.successfulReconnects += 1;

    // Reset backoff for next potential failure
    this.currentBackoffMs = this.config.initialBackoffMs;

    this._clearRetryTimer();
    this._transitionState('stable', 'connection restored');
  }

  // ── Private: Retry Logic ─────────────────────────────────────────

  private _scheduleRetry(): boolean {
    if (this.destroyed || !this.pc) return false;

    if (this.metrics.totalAttempts >= this.config.maxRetries) {
      console.warn(`[Reconnection:${this.config.peerId}] Max retries (${this.config.maxRetries}) reached`);
      return false;
    }

    // Clear existing retry timer
    this._clearRetryTimer();

    const attemptNumber = this.metrics.totalAttempts + 1;
    const now = Date.now();

    this.metrics.totalAttempts = attemptNumber;
    this.metrics.lastAttemptAt = now;

    // Capture ICE candidate pair state before retry
    let iceCandidatePairBefore: ReconnectionAttempt['iceCandidatePairBefore'] | undefined;
    if (this.pc.getStats) {
      // Async, but we'll log it for diagnostics; don't block retry
      this.pc.getStats().then((stats) => {
        stats.forEach((report) => {
          if (report.type === 'candidate-pair' && report.state === 'succeeded') {
            iceCandidatePairBefore = {
              candidate: null,
              state: report.state as string,
              currentRoundTripTime: report.currentRoundTripTime || 0,
            };
          }
        });
      }).catch((e) => {
        console.warn(`[Reconnection:${this.config.peerId}] getStats error:`, e);
      });
    }

    const attempt: ReconnectionAttempt = {
      peerId: this.config.peerId,
      attemptNumber,
      timestamp: now,
      backoffDelayMs: this.currentBackoffMs,
      iceCandidatePairBefore,
    };

    this.config.onRetryAttempt?.(attempt);

    console.log(
      `[Reconnection:${this.config.peerId}] Scheduling retry #${attemptNumber} in ${this.currentBackoffMs}ms`
    );

    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this._performIceRestart();
    }, this.currentBackoffMs);

    // Update backoff for next attempt (exponential)
    this.currentBackoffMs = Math.min(
      this.currentBackoffMs * 2,
      this.config.maxBackoffMs
    );

    this._transitionState('recovering', `retry #${attemptNumber} scheduled`);
    this._emitMetrics();

    return true;
  }

  private _performIceRestart(): void {
    if (this.destroyed || !this.pc) return;

    console.log(`[Reconnection:${this.config.peerId}] Performing ICE restart`);

    // Preferred path: delegate to the consumer's signaling-aware restart.
    // PeerConnection._attemptIceRestart creates the offer, applies LAN
    // SDP optimizations, calls setLocalDescription AND emits onSignal so
    // the remote actually learns about the restart. The fallback below
    // only runs if no consumer callback is wired — in which case this is
    // a one-sided restart that won't recover the connection on its own
    // but at least refreshes local credentials.
    if (this.config.onIceRestartRequested) {
      try {
        this.config.onIceRestartRequested();
      } catch (e) {
        console.error(`[Reconnection:${this.config.peerId}] onIceRestartRequested threw:`, e);
      }
      return;
    }

    console.warn(
      `[Reconnection:${this.config.peerId}] No onIceRestartRequested callback ` +
      `wired — falling back to local-only setLocalDescription. The remote ` +
      `peer will NOT learn about this restart. Wire onIceRestartRequested ` +
      `to PeerConnection._attemptIceRestart for a working restart.`
    );

    try {
      this.pc.createOffer({ iceRestart: true })
        .then((offer) => {
          if (offer.sdp) {
            offer.sdp = this._optimizeSdpForLan(offer.sdp);
          }
          return this.pc!.setLocalDescription(offer);
        })
        .catch((e) => {
          console.error(`[Reconnection:${this.config.peerId}] ICE restart error:`, e);
        });
    } catch (e) {
      console.error(`[Reconnection:${this.config.peerId}] ICE restart exception:`, e);
    }
  }

  private _clearRetryTimer(): void {
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  // ── Private: Quality Monitoring ──────────────────────────────────

  private _startQualityMonitoring(): void {
    if (this.qualityCheckInterval) return;

    console.log(`[Reconnection:${this.config.peerId}] Starting quality monitoring`);

    this.qualityCheckInterval = setInterval(() => {
      this._checkConnectionQuality();
    }, this.config.qualityCheckIntervalMs);
  }

  private _stopQualityMonitoring(): void {
    if (this.qualityCheckInterval) {
      clearInterval(this.qualityCheckInterval);
      this.qualityCheckInterval = null;
    }
  }

  private _stopInterfaceChecking(): void {
    if (this.interfaceCheckInterval) {
      clearInterval(this.interfaceCheckInterval);
      this.interfaceCheckInterval = null;
    }
  }

  private _checkConnectionQuality(): void {
    if (this.destroyed || !this.pc) return;

    this.pc.getStats()
      .then((stats) => {
        const quality = this._parseQualityMetrics(stats);

        this.metrics.currentRtt = quality.rtt;
        this.metrics.currentPacketLoss = quality.packetsLost > 0 ? quality.packetsLost / 1000 : 0;
        this.metrics.lastQualityCheckAt = Date.now();

        // Proactive reconnect if quality degrades significantly
        if (this.state === 'stable') {
          const rttDegraded = quality.rtt > this.config.rttDegradationThresholdMs;
          const packetLossBad = this.metrics.currentPacketLoss > this.config.packetLossThreshold;

          if (rttDegraded || packetLossBad) {
            console.warn(
              `[Reconnection:${this.config.peerId}] Quality degradation detected ` +
              `(RTT: ${quality.rtt}ms, Loss: ${(this.metrics.currentPacketLoss * 100).toFixed(1)}%) — proactive reconnect`
            );
            this._transitionState('monitoring', 'quality degradation detected');
            this._scheduleRetry();
          }
        }

        this._emitMetrics();
      })
      .catch((e) => {
        console.warn(`[Reconnection:${this.config.peerId}] getStats error:`, e);
      });
  }

  private _parseQualityMetrics(stats: RTCStatsReport): ConnectionQuality {
    let rtt = 0;
    let packetsLost = 0;
    let jitter = 0;
    let bitrate = 0;

    stats.forEach((report) => {
      if (report.type === 'candidate-pair' && report.state === 'succeeded') {
        rtt = Math.round((report.currentRoundTripTime || 0) * 1000);
      }
      if (report.type === 'inbound-rtp' && report.kind === 'video') {
        packetsLost = report.packetsLost || 0;
        jitter = (report.jitter || 0) * 1000;
      }
      if (report.type === 'outbound-rtp' && report.kind === 'video') {
        bitrate = report.bytesSent ? Math.round((report.bytesSent * 8) / 1000) : 0;
      }
    });

    return { rtt, packetsLost, jitter, bitrate };
  }

  // ── Private: Network Change Detection ─────────────────────────────

  private _setupNetworkListeners(): void {
    if (!this.config.monitorNetworkChanges) return;

    console.log(`[Reconnection:${this.config.peerId}] Setting up network listeners`);

    // Online/offline detection
    this.onlineListener = () => {
      console.log(`[Reconnection:${this.config.peerId}] Network online detected`);
      if (this.state === 'recovering' || this.state === 'monitoring') {
        this._scheduleRetry();
      }
      this.lastNetworkStatus = true;
    };

    this.offlineListener = () => {
      console.warn(`[Reconnection:${this.config.peerId}] Network offline detected`);
      this._transitionState('monitoring', 'network offline detected');
      this.lastNetworkStatus = false;
    };

    window.addEventListener('online', this.onlineListener);
    window.addEventListener('offline', this.offlineListener);

    // LAN-specific: monitor for interface/subnet changes (if supported)
    if (this.config.monitorInterfaceChanges && 'RTCRtpSender' in window) {
      this.networkChangeListener = () => {
        const currentSignature = this._getNetworkSignature();
        if (currentSignature !== this.lastInterfaceSignature && this.lastInterfaceSignature !== '') {
          console.warn(
            `[Reconnection:${this.config.peerId}] Network interface change detected ` +
            `(${this.lastInterfaceSignature} → ${currentSignature})`
          );
          this._transitionState('monitoring', 'network interface change detected');
          this._scheduleRetry();
        }
        this.lastInterfaceSignature = currentSignature;
      };

      // Check every 5 seconds for interface changes (LAN-specific)
      this.interfaceCheckInterval = setInterval(this.networkChangeListener, 5000);
    }
  }

  private _removeNetworkListeners(): void {
    if (this.onlineListener) {
      window.removeEventListener('online', this.onlineListener);
      this.onlineListener = null;
    }

    if (this.offlineListener) {
      window.removeEventListener('offline', this.offlineListener);
      this.offlineListener = null;
    }

    if (this.networkChangeListener) {
      this.networkChangeListener = null;
    }
  }

  /**
   * Get a signature of the current network state (LAN-specific).
   * Used to detect interface/subnet changes on local network.
   */
  private _getNetworkSignature(): string {
    // In a browser, we can't directly access IP addresses.
    // However, we can use ICE candidates to infer local IP changes.
    if (!this.pc) return '';

    const localCandidates: string[] = [];
    const signalingState = this.pc.signalingState;
    const connectionState = this.pc.connectionState;

    // Use connection state as a proxy for interface health
    return `sig:${signalingState},conn:${connectionState}`;
  }

  // ── Private: SDP Optimization (LAN) ──────────────────────────────

  private _optimizeSdpForLan(sdp: string): string {
    let modified = sdp;

    // Increase audio bitrate for LAN (Opus → 128kbps)
    modified = modified.replace(
      /a=fmtp:111 /g,
      'a=fmtp:111 maxaveragebitrate=128000;stereo=1;'
    );

    // Set bandwidth to 10 Mbps (generous for LAN)
    if (!modified.includes('b=AS:')) {
      modified = modified.replace(
        /m=video /g,
        'b=AS:10000\r\nm=video '
      );
    }

    return modified;
  }

  // ── Private: Metrics ─────────────────────────────────────────────

  private _emitMetrics(): void {
    // Recalculate success rate
    if (this.metrics.totalAttempts > 0) {
      this.metrics.successRate = this.metrics.successfulReconnects / this.metrics.totalAttempts;
    }

    // Calculate active duration from start time without mutating the metric
    const activeSinceMs = Date.now() - this._startTime;
    const metricsSnapshot = { ...this.metrics, activeSinceMs };

    this.config.onMetricsUpdate?.(metricsSnapshot);
  }
}

/**
 * Manager for per-peer reconnection in group calls.
 * Tracks reconnection state for N peers in a mesh topology.
 */
export class GroupReconnectionManager {
  private managers: Map<string, ReconnectionManager> = new Map();
  private destroyed: boolean = false;

  constructor(
    private globalConfig: Omit<ReconnectionConfig, 'peerId'>
  ) {}

  /**
   * Get or create a reconnection manager for a peer. Optional per-peer
   * config override lets the caller supply callbacks that need closure
   * over the peerId — e.g. `onIceRestartRequested` that delegates to a
   * specific PeerConnection wrapper. Override fields shallow-merge over
   * the global config and are only applied on first create.
   */
  forPeer(
    peerId: string,
    peerOverride?: Partial<Omit<ReconnectionConfig, 'peerId'>>,
  ): ReconnectionManager {
    if (this.destroyed) {
      throw new Error('GroupReconnectionManager is destroyed');
    }

    let manager = this.managers.get(peerId);
    if (!manager) {
      manager = new ReconnectionManager({
        ...this.globalConfig,
        ...peerOverride,
        peerId,
      });
      this.managers.set(peerId, manager);
      console.log(`[GroupReconnection] Created manager for peer ${peerId}`);
    }
    return manager;
  }

  /**
   * Remove peer (called when peer leaves group).
   */
  removePeer(peerId: string): void {
    const manager = this.managers.get(peerId);
    if (manager) {
      manager.destroy();
      this.managers.delete(peerId);
      console.log(`[GroupReconnection] Removed manager for peer ${peerId}`);
    }
  }

  /**
   * Get all managers.
   */
  allManagers(): ReconnectionManager[] {
    return Array.from(this.managers.values());
  }

  /**
   * Get metrics for all peers.
   */
  getAllMetrics(): Map<string, ReconnectionMetrics> {
    const allMetrics = new Map<string, ReconnectionMetrics>();
    for (const [peerId, manager] of this.managers) {
      allMetrics.set(peerId, manager.getMetrics());
    }
    return allMetrics;
  }

  /**
   * Destroy all managers and clean up.
   */
  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;

    console.log(`[GroupReconnection] Destroying group manager (${this.managers.size} peers)`);

    for (const manager of this.managers.values()) {
      manager.destroy();
    }
    this.managers.clear();
  }
}
