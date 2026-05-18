/**
 * HealthCheckSystem.ts — Phase 14: Unified Health Monitor
 *
 * Monitors 5 subsystems and produces a real-time health status for the
 * diagnostics screen and the troubleshooting export.
 *
 * ┌────────────────────────────────────────────────────────────────────────┐
 * │                      Health Check Architecture                         │
 * │                                                                        │
 * │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐   │
 * │  │   App   │  │ Backend │  │ Network │  │  Media  │  │Database │   │
 * │  │ Health  │  │ Health  │  │ Health  │  │ Health  │  │ Health  │   │
 * │  │         │  │         │  │         │  │         │  │         │   │
 * │  │• Memory │  │• HTTP   │  │• Socket │  │• Cam    │  │• Write  │   │
 * │  │• Render │  │• Socket │  │• Ping   │  │• Mic    │  │• Read   │   │
 * │  │• Errors │  │• API    │  │• Online │  │• Screen │  │• Size   │   │
 * │  │• Uptime │  │• Latency│  │• RTT    │  │• Codec  │  │• Integ. │   │
 * │  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘   │
 * │       │            │            │            │            │          │
 * │       └────────────┴────────────┼────────────┴────────────┘          │
 * │                                 ▼                                     │
 * │                    ┌─────────────────────┐                            │
 * │                    │  HealthAggregator    │                            │
 * │                    │                     │                            │
 * │                    │  Overall: ●● ●● ●●  │                            │
 * │                    │  healthy / degraded  │                            │
 * │                    │  / unhealthy / down  │                            │
 * │                    └─────────────────────┘                            │
 * │                                 │                                     │
 * │                    ┌────────────┴────────────┐                        │
 * │                    ▼                         ▼                        │
 * │            DiagnosticsScreen          DiagnosticsCollector             │
 * └────────────────────────────────────────────────────────────────────────┘
 *
 * Health status cascade:
 *   healthy   → all subsystems green
 *   degraded  → ≥1 subsystem warning but none critical
 *   unhealthy → ≥1 subsystem critical
 *   down      → app or backend completely unresponsive
 */

import { diagnosticsLogger, type CategoryLogger } from './DiagnosticsLogger';

// ── Types ───────────────────────────────────────────────────────

export type HealthStatus = 'healthy' | 'degraded' | 'unhealthy' | 'down' | 'unknown';

export type SubsystemName = 'app' | 'backend' | 'network' | 'media' | 'database';

export interface SubsystemHealth {
  /** Subsystem identifier */
  name: SubsystemName;
  /** Overall status for this subsystem */
  status: HealthStatus;
  /** Human-readable status message */
  message: string;
  /** Detailed check results */
  checks: HealthCheck[];
  /** Timestamp of last check */
  lastCheckedAt: number;
  /** Check duration (ms) */
  checkDurationMs: number;
  /** Consecutive failures */
  consecutiveFailures: number;
}

export interface HealthCheck {
  /** Check name (e.g. "memory_usage", "server_http") */
  name: string;
  /** Check status */
  status: HealthStatus;
  /** Descriptive message */
  message: string;
  /** Metric value if applicable */
  value?: number;
  /** Threshold that was checked against */
  threshold?: number;
  /** Unit for the value (ms, MB, %, count) */
  unit?: string;
}

export interface OverallHealth {
  /** Aggregated status across all subsystems */
  status: HealthStatus;
  /** Per-subsystem health */
  subsystems: Record<SubsystemName, SubsystemHealth>;
  /** Timestamp of this snapshot */
  timestamp: number;
  /** Summary message */
  message: string;
  /** Count of healthy / degraded / unhealthy subsystems */
  summary: { healthy: number; degraded: number; unhealthy: number; down: number; unknown: number };
}

/** Callback invoked whenever health changes */
export type HealthChangeCallback = (health: OverallHealth) => void;

// ── Configuration ───────────────────────────────────────────────

export interface HealthCheckConfig {
  /** How often to run health checks (ms) — default 10_000 */
  intervalMs: number;
  /** Timeout for each individual check (ms) — default 5_000 */
  checkTimeoutMs: number;
  /** Server base URL for backend checks. When unset, the runtime
   *  default is the URL the auth store currently has — i.e. the SAME
   *  endpoint the live socket talks to. Hard-coding a fallback would
   *  produce false-fail "server unhealthy" diagnostics whenever the
   *  app connected to a different host than the diagnostic suite
   *  expected. */
  serverBaseUrl: string;
  /** Maximum acceptable memory usage (MB) before warning */
  memoryWarningMB: number;
  /** Maximum acceptable memory usage (MB) before critical */
  memoryCriticalMB: number;
  /** Maximum acceptable RTT (ms) before warning */
  rttWarningMs: number;
  /** Maximum acceptable RTT (ms) before critical */
  rttCriticalMs: number;
  /** Maximum acceptable error rate (errors/min) before warning */
  errorRateWarning: number;
  /** Maximum acceptable error rate (errors/min) before critical */
  errorRateCritical: number;
  /** Maximum acceptable DB size (MB) before warning */
  dbSizeWarningMB: number;
}

const DEFAULT_HEALTH_CONFIG: HealthCheckConfig = {
  intervalMs: 10_000,
  checkTimeoutMs: 5_000,
  // Empty string = "use whatever auth.store currently has". The
  // _fetchHealth() resolver below falls back to this dynamically so
  // diagnostics always probe the SAME endpoint the live socket is
  // connected to. The legacy hard-coded :7420 default produced false
  // fail diagnostics whenever the operator deployed Helen on the
  // production port (3000/3088) — exactly the audit case.
  serverBaseUrl: '',
  memoryWarningMB: 400,
  memoryCriticalMB: 800,
  rttWarningMs: 100,
  rttCriticalMs: 500,
  errorRateWarning: 5,
  errorRateCritical: 20,
  dbSizeWarningMB: 500,
};

// ── Helper: Timeout Race ────────────────────────────────────────

function withTimeout<T>(promise: Promise<T>, ms: number, fallback: T): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>(resolve => setTimeout(() => resolve(fallback), ms)),
  ]);
}

// ── Health Check Engine ─────────────────────────────────────────

class HealthCheckEngine {
  private _config: HealthCheckConfig = { ...DEFAULT_HEALTH_CONFIG };
  private _intervalId: ReturnType<typeof setInterval> | null = null;
  private _listeners: HealthChangeCallback[] = [];
  private _lastHealth: OverallHealth | null = null;
  private _running = false;
  private _consecutiveFailures: Record<SubsystemName, number> = {
    app: 0, backend: 0, network: 0, media: 0, database: 0,
  };

  private readonly _log: CategoryLogger;

  // Externally injected state providers
  private _socketConnected: () => boolean = () => false;
  private _getErrorRate: () => number = () => 0;
  private _getAppUptime: () => number = () => 0;

  constructor() {
    this._log = diagnosticsLogger.createLogger('system', 'HealthCheck');
  }

  // ── Lifecycle ──────────────────────────────────────────

  /**
   * Start periodic health checks.
   */
  start(config?: Partial<HealthCheckConfig>): void {
    if (this._running) return;

    if (config) {
      this._config = { ...this._config, ...config };
    }

    this._running = true;
    this._log.info('Health check system started', { intervalMs: this._config.intervalMs });

    // Run first check immediately
    this.runAllChecks().catch(() => {});

    // Schedule periodic checks
    this._intervalId = setInterval(() => {
      this.runAllChecks().catch(() => {});
    }, this._config.intervalMs);
  }

  /**
   * Stop periodic health checks.
   */
  stop(): void {
    if (this._intervalId) {
      clearInterval(this._intervalId);
      this._intervalId = null;
    }
    this._running = false;
    this._log.info('Health check system stopped');
  }

  /**
   * Register external state providers for checks that need app state.
   */
  registerProviders(providers: {
    socketConnected?: () => boolean;
    getErrorRate?: () => number;
    getAppUptime?: () => number;
  }): void {
    if (providers.socketConnected) this._socketConnected = providers.socketConnected;
    if (providers.getErrorRate) this._getErrorRate = providers.getErrorRate;
    if (providers.getAppUptime) this._getAppUptime = providers.getAppUptime;
  }

  /**
   * Subscribe to health changes.
   */
  onChange(callback: HealthChangeCallback): () => void {
    this._listeners.push(callback);
    return () => {
      this._listeners = this._listeners.filter(l => l !== callback);
    };
  }

  /**
   * Get the most recent health snapshot.
   */
  getLastHealth(): OverallHealth | null {
    return this._lastHealth;
  }

  // ── Run All Checks ─────────────────────────────────────

  /**
   * Execute all subsystem checks and produce an OverallHealth snapshot.
   */
  async runAllChecks(): Promise<OverallHealth> {
    const subsystems: SubsystemName[] = ['app', 'backend', 'network', 'media', 'database'];

    const results = await Promise.all(
      subsystems.map(name => this._checkSubsystem(name)),
    );

    const subsystemMap: Record<SubsystemName, SubsystemHealth> = {} as any;
    const summary = { healthy: 0, degraded: 0, unhealthy: 0, down: 0, unknown: 0 };

    for (const result of results) {
      subsystemMap[result.name] = result;
      summary[result.status]++;
    }

    // Aggregate overall status
    let overallStatus: HealthStatus;
    let overallMessage: string;

    if (summary.down > 0) {
      overallStatus = 'down';
      overallMessage = `${summary.down} subsystem(s) down`;
    } else if (summary.unhealthy > 0) {
      overallStatus = 'unhealthy';
      overallMessage = `${summary.unhealthy} subsystem(s) unhealthy`;
    } else if (summary.degraded > 0) {
      overallStatus = 'degraded';
      overallMessage = `${summary.degraded} subsystem(s) degraded`;
    } else if (summary.unknown > 0) {
      overallStatus = 'unknown';
      overallMessage = `${summary.unknown} subsystem(s) status unknown`;
    } else {
      overallStatus = 'healthy';
      overallMessage = 'All systems operational';
    }

    const health: OverallHealth = {
      status: overallStatus,
      subsystems: subsystemMap,
      timestamp: Date.now(),
      message: overallMessage,
      summary,
    };

    // Detect status change
    const prevStatus = this._lastHealth?.status;
    this._lastHealth = health;

    if (prevStatus && prevStatus !== overallStatus) {
      this._log.warn('Overall health changed', { from: prevStatus, to: overallStatus });
    }

    // Notify listeners
    for (const listener of this._listeners) {
      try {
        listener(health);
      } catch (err) {
        this._log.error('Health listener error', err as Error);
      }
    }

    return health;
  }

  // ── Private: Subsystem Checks ──────────────────────────

  private async _checkSubsystem(name: SubsystemName): Promise<SubsystemHealth> {
    const start = performance.now();
    let checks: HealthCheck[] = [];

    try {
      switch (name) {
        case 'app':      checks = await this._checkApp(); break;
        case 'backend':  checks = await this._checkBackend(); break;
        case 'network':  checks = await this._checkNetwork(); break;
        case 'media':    checks = await this._checkMedia(); break;
        case 'database': checks = await this._checkDatabase(); break;
      }
      this._consecutiveFailures[name] = 0;
    } catch (err) {
      this._consecutiveFailures[name]++;
      checks = [{
        name: 'check_execution',
        status: 'unhealthy',
        message: `Check threw: ${(err as Error).message}`,
      }];
    }

    const duration = Math.round(performance.now() - start);

    // Derive subsystem status from individual checks
    const status = this._deriveStatus(checks);
    const message = checks
      .filter(c => c.status !== 'healthy')
      .map(c => c.message)
      .join('; ') || 'All checks passed';

    return {
      name,
      status,
      message,
      checks,
      lastCheckedAt: Date.now(),
      checkDurationMs: duration,
      consecutiveFailures: this._consecutiveFailures[name],
    };
  }

  private _deriveStatus(checks: HealthCheck[]): HealthStatus {
    if (checks.length === 0) return 'unknown';
    const hasDown = checks.some(c => c.status === 'down');
    const hasUnhealthy = checks.some(c => c.status === 'unhealthy');
    const hasDegraded = checks.some(c => c.status === 'degraded');
    if (hasDown) return 'down';
    if (hasUnhealthy) return 'unhealthy';
    if (hasDegraded) return 'degraded';
    return 'healthy';
  }

  // ── App Health ─────────────────────────────────────────

  private async _checkApp(): Promise<HealthCheck[]> {
    const checks: HealthCheck[] = [];

    // 1. Memory usage
    if (typeof performance !== 'undefined' && (performance as any).memory) {
      const mem = (performance as any).memory;
      const usedMB = Math.round(mem.usedJSHeapSize / (1024 * 1024));
      const totalMB = Math.round(mem.totalJSHeapSize / (1024 * 1024));
      const limitMB = Math.round(mem.jsHeapSizeLimit / (1024 * 1024));

      let status: HealthStatus = 'healthy';
      let message = `${usedMB}MB / ${totalMB}MB heap`;
      if (usedMB > this._config.memoryCriticalMB) {
        status = 'unhealthy';
        message = `High memory: ${usedMB}MB (critical > ${this._config.memoryCriticalMB}MB)`;
      } else if (usedMB > this._config.memoryWarningMB) {
        status = 'degraded';
        message = `Elevated memory: ${usedMB}MB (warning > ${this._config.memoryWarningMB}MB)`;
      }

      checks.push({ name: 'memory_usage', status, message, value: usedMB, threshold: this._config.memoryWarningMB, unit: 'MB' });
    } else {
      checks.push({ name: 'memory_usage', status: 'unknown', message: 'performance.memory not available' });
    }

    // 2. Error rate
    const errorRate = this._getErrorRate();
    let errStatus: HealthStatus = 'healthy';
    let errMsg = `${errorRate.toFixed(1)} errors/min`;
    if (errorRate > this._config.errorRateCritical) {
      errStatus = 'unhealthy';
      errMsg = `High error rate: ${errorRate.toFixed(1)}/min (critical > ${this._config.errorRateCritical})`;
    } else if (errorRate > this._config.errorRateWarning) {
      errStatus = 'degraded';
      errMsg = `Elevated error rate: ${errorRate.toFixed(1)}/min (warning > ${this._config.errorRateWarning})`;
    }
    checks.push({ name: 'error_rate', status: errStatus, message: errMsg, value: errorRate, threshold: this._config.errorRateWarning, unit: 'errors/min' });

    // 3. Uptime
    const uptimeMs = this._getAppUptime();
    checks.push({
      name: 'uptime',
      status: 'healthy',
      message: `Uptime: ${Math.round(uptimeMs / 1000)}s`,
      value: uptimeMs,
      unit: 'ms',
    });

    // 4. Renderer responsiveness (frame drop proxy)
    const renderCheck = await this._checkRendererResponsiveness();
    checks.push(renderCheck);

    return checks;
  }

  private _checkRendererResponsiveness(): Promise<HealthCheck> {
    return new Promise(resolve => {
      const start = performance.now();
      requestAnimationFrame(() => {
        const frameTime = performance.now() - start;
        let status: HealthStatus = 'healthy';
        let message = `Frame callback: ${frameTime.toFixed(1)}ms`;
        if (frameTime > 100) {
          status = 'unhealthy';
          message = `Renderer blocked: ${frameTime.toFixed(1)}ms (>100ms)`;
        } else if (frameTime > 33) {
          status = 'degraded';
          message = `Renderer slow: ${frameTime.toFixed(1)}ms (>33ms)`;
        }
        resolve({ name: 'renderer_responsiveness', status, message, value: Math.round(frameTime), unit: 'ms' });
      });
    });
  }

  // ── Backend Health ─────────────────────────────────────

  private async _checkBackend(): Promise<HealthCheck[]> {
    const checks: HealthCheck[] = [];

    // 1. HTTP health endpoint
    const httpCheck = await withTimeout(
      this._fetchHealth(),
      this._config.checkTimeoutMs,
      { name: 'server_http', status: 'down' as HealthStatus, message: 'Health check timed out' },
    );
    checks.push(httpCheck);

    // 2. Socket.IO connection
    const socketConnected = this._socketConnected();
    checks.push({
      name: 'server_socket',
      status: socketConnected ? 'healthy' : 'unhealthy',
      message: socketConnected ? 'Socket.IO connected' : 'Socket.IO disconnected',
    });

    return checks;
  }

  private async _fetchHealth(): Promise<HealthCheck> {
    try {
      // Resolve the actual base URL at probe time — config-supplied
      // override wins, otherwise we follow whatever the auth store
      // says is currently in use. Lazy import avoids a renderer
      // dependency cycle (diagnostics → auth.store → ... → diagnostics).
      let baseUrl = this._config.serverBaseUrl;
      if (!baseUrl) {
        try {
          const mod = await import('@/stores/auth.store');
          baseUrl = mod.useAuthStore.getState().serverUrl || '';
        } catch {
          baseUrl = '';
        }
      }
      if (!baseUrl) {
        return {
          name: 'server_http',
          status: 'unhealthy',
          message: 'no server URL configured',
        };
      }
      const start = performance.now();
      const response = await fetch(`${baseUrl}/api/health`, {
        method: 'GET',
        signal: AbortSignal.timeout(this._config.checkTimeoutMs),
      });
      const rttMs = Math.round(performance.now() - start);

      if (!response.ok) {
        return {
          name: 'server_http',
          status: 'unhealthy',
          message: `Server returned HTTP ${response.status}`,
          value: response.status,
        };
      }

      let status: HealthStatus = 'healthy';
      let message = `Server OK (${rttMs}ms)`;
      if (rttMs > this._config.rttCriticalMs) {
        status = 'unhealthy';
        message = `Server slow: ${rttMs}ms (critical > ${this._config.rttCriticalMs}ms)`;
      } else if (rttMs > this._config.rttWarningMs) {
        status = 'degraded';
        message = `Server latency elevated: ${rttMs}ms (warning > ${this._config.rttWarningMs}ms)`;
      }

      return { name: 'server_http', status, message, value: rttMs, threshold: this._config.rttWarningMs, unit: 'ms' };
    } catch (err) {
      return {
        name: 'server_http',
        status: 'down',
        message: `Server unreachable: ${(err as Error).message}`,
      };
    }
  }

  // ── Network Health ─────────────────────────────────────

  private async _checkNetwork(): Promise<HealthCheck[]> {
    const checks: HealthCheck[] = [];

    // 1. Navigator online status
    const online = typeof navigator !== 'undefined' ? navigator.onLine : true;
    checks.push({
      name: 'navigator_online',
      status: online ? 'healthy' : 'down',
      message: online ? 'Browser reports online' : 'Browser reports offline',
    });

    // 2. Socket.IO connection (same as backend but from network perspective)
    const socketOk = this._socketConnected();
    checks.push({
      name: 'socket_connection',
      status: socketOk ? 'healthy' : 'unhealthy',
      message: socketOk ? 'Real-time connection active' : 'Real-time connection lost',
    });

    // 3. Network type (if available)
    if (typeof navigator !== 'undefined' && (navigator as any).connection) {
      const conn = (navigator as any).connection;
      const effectiveType = conn.effectiveType || 'unknown';
      const downlink = conn.downlink || 0;

      let status: HealthStatus = 'healthy';
      let message = `Network type: ${effectiveType}, downlink: ${downlink}Mbps`;
      if (effectiveType === '2g' || effectiveType === 'slow-2g') {
        status = 'degraded';
        message = `Slow network detected: ${effectiveType}`;
      }

      checks.push({ name: 'network_quality', status, message, value: downlink, unit: 'Mbps' });
    }

    return checks;
  }

  // ── Media Health ───────────────────────────────────────

  private async _checkMedia(): Promise<HealthCheck[]> {
    const checks: HealthCheck[] = [];

    // Check if mediaDevices API is available
    if (typeof navigator === 'undefined' || !navigator.mediaDevices) {
      checks.push({
        name: 'media_api',
        status: 'unknown',
        message: 'MediaDevices API not available',
      });
      return checks;
    }

    // 1. Enumerate devices (non-intrusive — does NOT request permissions)
    try {
      const devices = await withTimeout(
        navigator.mediaDevices.enumerateDevices(),
        3_000,
        [],
      );

      const audioInputs = devices.filter(d => d.kind === 'audioinput');
      const videoInputs = devices.filter(d => d.kind === 'videoinput');
      const audioOutputs = devices.filter(d => d.kind === 'audiooutput');

      checks.push({
        name: 'audio_input_devices',
        status: audioInputs.length > 0 ? 'healthy' : 'degraded',
        message: `${audioInputs.length} microphone(s) detected`,
        value: audioInputs.length,
        unit: 'devices',
      });

      checks.push({
        name: 'video_input_devices',
        status: videoInputs.length > 0 ? 'healthy' : 'degraded',
        message: `${videoInputs.length} camera(s) detected`,
        value: videoInputs.length,
        unit: 'devices',
      });

      checks.push({
        name: 'audio_output_devices',
        status: audioOutputs.length > 0 ? 'healthy' : 'degraded',
        message: `${audioOutputs.length} speaker(s) detected`,
        value: audioOutputs.length,
        unit: 'devices',
      });
    } catch (err) {
      checks.push({
        name: 'device_enumeration',
        status: 'unhealthy',
        message: `Failed to enumerate devices: ${(err as Error).message}`,
      });
    }

    // 2. WebRTC support
    const rtcSupported = typeof RTCPeerConnection !== 'undefined';
    checks.push({
      name: 'webrtc_support',
      status: rtcSupported ? 'healthy' : 'unhealthy',
      message: rtcSupported ? 'WebRTC supported' : 'WebRTC not available',
    });

    return checks;
  }

  // ── Database Health ────────────────────────────────────

  private async _checkDatabase(): Promise<HealthCheck[]> {
    const checks: HealthCheck[] = [];

    // 1. localStorage availability and quota
    try {
      const testKey = '__hc_test_' + Date.now();
      localStorage.setItem(testKey, 'ok');
      localStorage.removeItem(testKey);

      // Estimate usage
      let totalSize = 0;
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key) {
          totalSize += key.length + (localStorage.getItem(key)?.length || 0);
        }
      }
      const sizeMB = (totalSize * 2) / (1024 * 1024); // UTF-16

      let status: HealthStatus = 'healthy';
      let message = `localStorage: ${sizeMB.toFixed(2)}MB used`;
      if (sizeMB > 4) {
        status = 'degraded';
        message = `localStorage near quota: ${sizeMB.toFixed(2)}MB (max ~5MB)`;
      }

      checks.push({ name: 'localstorage', status, message, value: parseFloat(sizeMB.toFixed(2)), unit: 'MB' });
    } catch (err) {
      checks.push({
        name: 'localstorage',
        status: 'unhealthy',
        message: `localStorage unavailable: ${(err as Error).message}`,
      });
    }

    // 2. Backend database health (via API)
    const dbCheck = await withTimeout(
      this._fetchDatabaseHealth(),
      this._config.checkTimeoutMs,
      { name: 'server_database', status: 'unknown' as HealthStatus, message: 'DB health check timed out' },
    );
    checks.push(dbCheck);

    // 3. IndexedDB availability
    try {
      const idbAvailable = typeof indexedDB !== 'undefined';
      checks.push({
        name: 'indexeddb',
        status: idbAvailable ? 'healthy' : 'degraded',
        message: idbAvailable ? 'IndexedDB available' : 'IndexedDB not available',
      });
    } catch {
      checks.push({ name: 'indexeddb', status: 'unknown', message: 'IndexedDB check failed' });
    }

    return checks;
  }

  private async _fetchDatabaseHealth(): Promise<HealthCheck> {
    try {
      const response = await fetch(`${this._config.serverBaseUrl}/api/health`, {
        method: 'GET',
        signal: AbortSignal.timeout(this._config.checkTimeoutMs),
      });

      if (!response.ok) {
        return {
          name: 'server_database',
          status: 'unhealthy',
          message: `Server DB check failed (HTTP ${response.status})`,
        };
      }

      const data = await response.json();
      // Expect backend to report DB status in health response
      const dbStatus = data?.database || data?.db;
      if (dbStatus === 'ok' || dbStatus === true) {
        return { name: 'server_database', status: 'healthy', message: 'Server database operational' };
      }

      return {
        name: 'server_database',
        status: dbStatus ? 'degraded' : 'healthy',
        message: dbStatus ? `Server DB: ${dbStatus}` : 'Server database status assumed OK',
      };
    } catch (err) {
      return {
        name: 'server_database',
        status: 'unknown',
        message: `Cannot verify server DB: ${(err as Error).message}`,
      };
    }
  }
}

// ── Singleton Export ────────────────────────────────────────────

export const healthCheckSystem = new HealthCheckEngine();
