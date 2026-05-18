/**
 * ConnectionResilience.ts — Bulletproof connectivity for non-technical users.
 *
 * This service wraps around the existing socket.manager and discovery system
 * to provide a SIMPLE, FRIENDLY connection state that the UI can show without
 * any technical jargon.
 *
 * Problem: The current system exposes ~15 different connection states
 * (socket phases, discovery phases, backend health, auth state, etc.)
 * which are meaningless to a child or non-technical user.
 *
 * Solution: Collapse all states into 5 user-friendly connection states:
 *   1. "connected"    → Everything works. Green dot. No banner.
 *   2. "connecting"   → App is starting up or recovering. Blue spinner.
 *   3. "slow"         → Connected but laggy. Yellow warning.
 *   4. "offline"      → Lost connection. Red banner with auto-retry.
 *   5. "no_server"    → Can't find server at all. Help screen.
 *
 * Features:
 *   - Friendly status messages (no IPs, ports, or error codes)
 *   - Auto-retry with exponential backoff (invisible to user)
 *   - Connection quality estimation (RTT-based)
 *   - WiFi sleep detection (Windows power management)
 *   - Graceful degradation messages
 *   - Sound cues (optional) for state changes
 *   - One-tap "Try Again" that does everything needed
 */

import { AppLogger } from '../AppLogger';

const log = AppLogger.create('ConnectionResilience');

// ── Types ───────────────────────────────────────────────────

export type FriendlyConnectionState =
  | 'connected'
  | 'connecting'
  | 'slow'
  | 'offline'
  | 'no_server';

export interface ConnectionStatus {
  state: FriendlyConnectionState;
  messageKey: string;       // i18n key for user-facing message
  hintKey: string | null;   // i18n key for help hint (shown below message)
  canRetry: boolean;
  autoRetrying: boolean;
  retryCountdown: number;   // seconds until next auto-retry (0 = not retrying)
  rttMs: number | null;     // last known round-trip time
  since: number;            // timestamp when this state started
}

// ── Constants ───────────────────────────────────────────────

const RTT_SLOW_THRESHOLD = 150;         // ms — above this = "slow" state
const RTT_PROBE_INTERVAL = 5000;        // ms — how often to probe RTT
const MAX_AUTO_RETRIES = 10;            // cap auto-retries
const BASE_RETRY_DELAY = 3000;          // ms — first retry delay
const MAX_RETRY_DELAY = 30000;          // ms — maximum retry delay
const WIFI_SLEEP_CHECK_INTERVAL = 10000; // ms — check for WiFi sleep
const STABLE_DURATION = 5000;           // ms — consider connection stable after this

// ── State Messages (i18n keys) ──────────────────────────────

const STATE_MESSAGES: Record<FriendlyConnectionState, { messageKey: string; hintKey: string | null }> = {
  connected:  { messageKey: 'conn.connected',    hintKey: null },
  connecting: { messageKey: 'conn.connecting',    hintKey: 'conn.connecting_hint' },
  slow:       { messageKey: 'conn.slow',          hintKey: 'conn.slow_hint' },
  offline:    { messageKey: 'conn.offline',       hintKey: 'conn.offline_hint' },
  no_server:  { messageKey: 'conn.no_server',     hintKey: 'conn.no_server_hint' },
};

// ── Event Types ─────────────────────────────────────────────

type ConnectionListener = (status: ConnectionStatus) => void;

// ── Main Service ────────────────────────────────────────────

class ConnectionResilienceService {
  private status: ConnectionStatus;
  private listeners: ConnectionListener[] = [];
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private retryCount = 0;
  private rttProbeTimer: ReturnType<typeof setInterval> | null = null;
  private wifiSleepTimer: ReturnType<typeof setInterval> | null = null;
  private lastActivityTimestamp = Date.now();
  private serverUrl: string = '';
  private isRunning = false;

  constructor() {
    this.status = {
      state: 'connecting',
      messageKey: STATE_MESSAGES.connecting.messageKey,
      hintKey: STATE_MESSAGES.connecting.hintKey,
      canRetry: false,
      autoRetrying: false,
      retryCountdown: 0,
      rttMs: null,
      since: Date.now(),
    };
  }

  // ── Lifecycle ───────────────────────────────────────────

  /**
   * Start monitoring. Call once after app init.
   */
  start(serverUrl: string): void {
    this.serverUrl = serverUrl;
    this.isRunning = true;

    // Start RTT probing
    this.rttProbeTimer = setInterval(() => this.probeRTT(), RTT_PROBE_INTERVAL);

    // Start WiFi sleep detection
    this.wifiSleepTimer = setInterval(() => this.checkWifiSleep(), WIFI_SLEEP_CHECK_INTERVAL);

    // Initial probe
    this.probeRTT();

    log.info('Connection resilience started', { serverUrl });
  }

  /**
   * Stop monitoring.
   */
  stop(): void {
    this.isRunning = false;
    if (this.retryTimer) { clearTimeout(this.retryTimer); this.retryTimer = null; }
    if (this.rttProbeTimer) { clearInterval(this.rttProbeTimer); this.rttProbeTimer = null; }
    if (this.wifiSleepTimer) { clearInterval(this.wifiSleepTimer); this.wifiSleepTimer = null; }
  }

  /**
   * Update server URL (e.g., after discovery finds a new server).
   */
  updateServerUrl(url: string): void {
    this.serverUrl = url;
  }

  // ── External Signal Inputs ──────────────────────────────
  // These are called by other services (socket, discovery, etc.)
  // to feed state into the resilience engine.

  /**
   * Socket.IO connected successfully.
   */
  notifySocketConnected(): void {
    this.retryCount = 0;
    this.cancelRetry();
    this.transition('connected');
    log.info('Socket connected');
  }

  /**
   * Socket.IO disconnected.
   */
  notifySocketDisconnected(reason?: string): void {
    log.warn('Socket disconnected', { reason });
    if (this.status.state === 'connected' || this.status.state === 'slow') {
      this.transition('offline');
      this.startAutoRetry();
    }
  }

  /**
   * Socket.IO reconnection failed after all attempts.
   */
  notifyReconnectFailed(): void {
    log.error('Socket reconnection exhausted');
    this.transition('no_server');
    this.cancelRetry();
  }

  /**
   * Discovery found no servers after searching.
   */
  notifyNoServerFound(): void {
    if (this.status.state === 'connecting') {
      this.transition('no_server');
    }
  }

  /**
   * Discovery found a server.
   */
  notifyServerFound(url: string): void {
    this.serverUrl = url;
    if (this.status.state === 'no_server' || this.status.state === 'connecting') {
      this.transition('connecting');
    }
  }

  /**
   * Manual "Try Again" from user.
   */
  async retryNow(): Promise<void> {
    log.info('User-initiated retry');
    this.retryCount = 0;
    this.cancelRetry();
    this.transition('connecting');
    await this.probeRTT();
  }

  // ── RTT Probing ─────────────────────────────────────────

  private async probeRTT(): Promise<void> {
    if (!this.serverUrl || !this.isRunning) return;

    const start = performance.now();
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000);

      const res = await fetch(`${this.serverUrl}/api/health`, {
        signal: controller.signal,
        cache: 'no-store',
      });
      clearTimeout(timeoutId);

      if (!res.ok) throw new Error(`Health check returned ${res.status}`);

      const rtt = Math.round(performance.now() - start);
      this.status.rttMs = rtt;
      this.lastActivityTimestamp = Date.now();

      // Determine state based on RTT
      if (this.status.state === 'offline' || this.status.state === 'no_server' || this.status.state === 'connecting') {
        // Server is reachable — transition to connected or slow
        this.retryCount = 0;
        this.cancelRetry();
        this.transition(rtt > RTT_SLOW_THRESHOLD ? 'slow' : 'connected');
      } else if (this.status.state === 'connected' && rtt > RTT_SLOW_THRESHOLD) {
        this.transition('slow');
      } else if (this.status.state === 'slow' && rtt <= RTT_SLOW_THRESHOLD) {
        this.transition('connected');
      }
    } catch {
      // Health check failed
      if (this.status.state === 'connected' || this.status.state === 'slow') {
        this.transition('offline');
        this.startAutoRetry();
      }
    }
  }

  // ── WiFi Sleep Detection ────────────────────────────────

  private checkWifiSleep(): void {
    const now = Date.now();
    const gap = now - this.lastActivityTimestamp;

    // If there's been no network activity for > 30 seconds,
    // the WiFi adapter may have gone to sleep.
    if (gap > 30000 && this.status.state === 'connected') {
      log.warn('Possible WiFi sleep detected, probing...');
      this.probeRTT();
    }
  }

  // ── Auto-Retry ──────────────────────────────────────────

  private startAutoRetry(): void {
    if (this.retryCount >= MAX_AUTO_RETRIES) {
      log.warn('Max auto-retries reached');
      this.transition('no_server');
      return;
    }

    this.retryCount++;
    const delay = Math.min(BASE_RETRY_DELAY * Math.pow(1.5, this.retryCount - 1), MAX_RETRY_DELAY);

    this.status.autoRetrying = true;
    this.status.retryCountdown = Math.ceil(delay / 1000);
    this.notifyListeners();

    // Countdown tick
    const countdownInterval = setInterval(() => {
      if (this.status.retryCountdown > 0) {
        this.status.retryCountdown--;
        this.notifyListeners();
      } else {
        clearInterval(countdownInterval);
      }
    }, 1000);

    this.retryTimer = setTimeout(async () => {
      clearInterval(countdownInterval);
      this.status.autoRetrying = false;
      this.status.retryCountdown = 0;
      log.info(`Auto-retry attempt ${this.retryCount}/${MAX_AUTO_RETRIES}`);
      await this.probeRTT();
    }, delay);
  }

  private cancelRetry(): void {
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.status.autoRetrying = false;
    this.status.retryCountdown = 0;
  }

  // ── State Transition ────────────────────────────────────

  private transition(newState: FriendlyConnectionState): void {
    if (this.status.state === newState) return;

    const prev = this.status.state;
    const msgs = STATE_MESSAGES[newState];

    this.status = {
      ...this.status,
      state: newState,
      messageKey: msgs.messageKey,
      hintKey: msgs.hintKey,
      canRetry: newState === 'offline' || newState === 'no_server',
      since: Date.now(),
    };

    log.info(`Connection state: ${prev} → ${newState}`);
    this.notifyListeners();
  }

  // ── Subscriptions ───────────────────────────────────────

  getStatus(): ConnectionStatus {
    return { ...this.status };
  }

  onChange(callback: ConnectionListener): () => void {
    this.listeners.push(callback);
    // Immediately fire with current state
    callback({ ...this.status });
    return () => {
      this.listeners = this.listeners.filter((cb) => cb !== callback);
    };
  }

  private notifyListeners(): void {
    const snap = { ...this.status };
    for (const cb of this.listeners) {
      try { cb(snap); } catch {}
    }
  }
}

// ── Singleton ───────────────────────────────────────────────

export const connectionResilience = new ConnectionResilienceService();
