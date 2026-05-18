/**
 * StartupOptimizer.ts — Cold-start acceleration & lazy-loading orchestrator.
 *
 * Problem: Current startup path takes 5-15 seconds for returning users.
 * Critical path: Splash(1.2s) → Backend(0-8s) → Discovery(0-6s) → Session(1-3s)
 *
 * Optimizations:
 *   1. Parallel initialization: backend check + discovery run concurrently
 *   2. Cached server URL: skip discovery entirely if last-known server responds
 *   3. Deferred service init: non-critical services start AFTER UI is interactive
 *   4. Preload hints: begin loading heavy modules during splash animation
 *   5. Startup metrics: track and report cold-start timing for profiling
 *   6. Warm cache: pre-populate frequently-accessed data during idle time
 *
 * Integration:
 *   Called from AppBootstrapScreen before phase transitions.
 *   Does NOT replace existing startup — wraps and accelerates it.
 *
 * Target: Reduce returning-user startup from ~7s → ~2-3s
 */

// ── Types ───────────────────────────────────────────────────

export interface StartupTiming {
  /** Total time from renderer load to app ready (ms) */
  totalMs: number;
  /** Time spent in splash phase */
  splashMs: number;
  /** Time spent checking backend health */
  backendCheckMs: number;
  /** Time spent in discovery phase */
  discoveryMs: number;
  /** Time spent restoring session */
  sessionRestoreMs: number;
  /** Time spent initializing services after ready */
  deferredInitMs: number;
  /** Whether cached server URL was used (fast path) */
  usedCachedServer: boolean;
  /** Whether discovery was skipped entirely */
  discoverySkipped: boolean;
  /** Number of deferred service initializations */
  deferredServiceCount: number;
  /** Timestamp when app became interactive */
  interactiveAt: number;
}

export interface PreloadHint {
  /** Module path or chunk name */
  module: string;
  /** Priority: 'critical' loads during splash, 'normal' loads after interactive */
  priority: 'critical' | 'normal' | 'low';
  /** Whether this hint has been consumed */
  loaded: boolean;
}

type TimingCallback = (timing: StartupTiming) => void;

// ── Constants ───────────────────────────────────────────────

const CACHE_KEY_SERVER_URL = 'commclient_last_server_url';
const CACHE_KEY_SERVER_HEALTH_TS = 'commclient_last_server_health_ts';
const CACHE_KEY_STARTUP_TIMING = 'commclient_last_startup_timing';
const SERVER_CACHE_MAX_AGE_MS = 24 * 60 * 60 * 1_000; // 24 hours
const FAST_HEALTH_TIMEOUT_MS = 2_000;  // Quick check for cached server
const DEFERRED_INIT_DELAY_MS = 500;    // Wait after ready before deferred inits
const IDLE_PRELOAD_DELAY_MS = 2_000;   // Wait after interactive before preloading

// ── StartupOptimizer ────────────────────────────────────────

export class StartupOptimizer {
  private _timing: Partial<StartupTiming> = {};
  private _startTime = 0;
  private _phaseStart = 0;
  private _deferredQueue: Array<{ name: string; init: () => Promise<void> | void }> = [];
  private _preloadHints: PreloadHint[] = [];
  private _listeners: TimingCallback[] = [];
  private _destroyed = false;

  // ── Lifecycle ─────────────────────────────────────────────

  /**
   * Mark the beginning of the startup sequence.
   * Call this as early as possible (main.tsx or App.tsx mount).
   */
  markStart(): void {
    this._startTime = performance.now();
    this._timing = {
      usedCachedServer: false,
      discoverySkipped: false,
      deferredServiceCount: 0,
    };
  }

  /**
   * Mark the beginning of a startup phase.
   */
  markPhaseStart(phase: 'splash' | 'backendCheck' | 'discovery' | 'sessionRestore' | 'deferredInit'): void {
    this._phaseStart = performance.now();
  }

  /**
   * Mark the end of a startup phase and record its duration.
   */
  markPhaseEnd(phase: 'splash' | 'backendCheck' | 'discovery' | 'sessionRestore' | 'deferredInit'): void {
    const duration = Math.round(performance.now() - this._phaseStart);
    switch (phase) {
      case 'splash': this._timing.splashMs = duration; break;
      case 'backendCheck': this._timing.backendCheckMs = duration; break;
      case 'discovery': this._timing.discoveryMs = duration; break;
      case 'sessionRestore': this._timing.sessionRestoreMs = duration; break;
      case 'deferredInit': this._timing.deferredInitMs = duration; break;
    }
  }

  /**
   * Mark the app as interactive (user can see and use the UI).
   */
  markInteractive(): void {
    this._timing.interactiveAt = Date.now();
    this._timing.totalMs = Math.round(performance.now() - this._startTime);

    // Save timing for profiling
    this._saveTiming();

    // Emit timing to listeners
    const timing = this.getTiming();
    for (const cb of this._listeners) {
      try { cb(timing); } catch {}
    }

    // Start deferred initializations after a brief pause
    if (!this._destroyed) {
      setTimeout(() => this._runDeferredQueue(), DEFERRED_INIT_DELAY_MS);
    }

    // Preload low-priority modules during idle
    if (!this._destroyed) {
      setTimeout(() => this._runIdlePreloads(), IDLE_PRELOAD_DELAY_MS);
    }
  }

  destroy(): void {
    this._destroyed = true;
    this._deferredQueue = [];
    this._preloadHints = [];
    this._listeners = [];
  }

  // ── Fast Path: Cached Server ──────────────────────────────

  /**
   * Attempt to use the last-known server URL to skip discovery.
   * Returns the URL if the cached server responds within 2 seconds,
   * or null if we need to fall back to full discovery.
   */
  async tryCachedServer(): Promise<string | null> {
    try {
      const cachedUrl = localStorage.getItem(CACHE_KEY_SERVER_URL);
      const cachedTs = localStorage.getItem(CACHE_KEY_SERVER_HEALTH_TS);

      if (!cachedUrl) return null;

      // Check cache age
      if (cachedTs) {
        const age = Date.now() - parseInt(cachedTs, 10);
        if (age > SERVER_CACHE_MAX_AGE_MS) {
          // Cache too old, clear it
          this._clearServerCache();
          return null;
        }
      }

      // Quick health check with tight timeout
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), FAST_HEALTH_TIMEOUT_MS);

      try {
        const resp = await fetch(`${cachedUrl}/api/health`, {
          signal: controller.signal,
          cache: 'no-store',
        });
        clearTimeout(timeout);

        if (resp.ok) {
          this._timing.usedCachedServer = true;
          this._timing.discoverySkipped = true;
          // Refresh cache timestamp
          localStorage.setItem(CACHE_KEY_SERVER_HEALTH_TS, String(Date.now()));
          return cachedUrl;
        }
      } catch {
        clearTimeout(timeout);
      }

      return null;
    } catch {
      return null;
    }
  }

  /**
   * Cache a successfully connected server URL for future fast-path.
   */
  cacheServerUrl(url: string): void {
    try {
      localStorage.setItem(CACHE_KEY_SERVER_URL, url);
      localStorage.setItem(CACHE_KEY_SERVER_HEALTH_TS, String(Date.now()));
    } catch {}
  }

  // ── Deferred Service Initialization ───────────────────────

  /**
   * Register a service for deferred initialization.
   * These services start AFTER the UI is interactive.
   */
  deferInit(name: string, init: () => Promise<void> | void): void {
    this._deferredQueue.push({ name, init });
  }

  /**
   * Register a module for preloading during idle time.
   */
  addPreloadHint(module: string, priority: PreloadHint['priority'] = 'normal'): void {
    this._preloadHints.push({ module, priority, loaded: false });
  }

  // ── Parallel Initialization Helper ────────────────────────

  /**
   * Run backend check and discovery in parallel instead of sequentially.
   * Returns { serverUrl, fromCache } or throws if neither succeeds.
   */
  async parallelBootstrap(
    checkBackend: (url: string) => Promise<boolean>,
    runDiscovery: () => Promise<string | null>,
    fallbackUrl: string,
  ): Promise<{ serverUrl: string; fromCache: boolean }> {
    // First: try cached server (fast path)
    const cached = await this.tryCachedServer();
    if (cached) {
      return { serverUrl: cached, fromCache: true };
    }

    // Second: run backend check and discovery in parallel
    const results = await Promise.allSettled([
      // Try the fallback/localhost URL
      checkBackend(fallbackUrl).then(ok => ok ? fallbackUrl : null),
      // Try LAN discovery
      runDiscovery(),
    ]);

    // Pick the first successful result
    for (const result of results) {
      if (result.status === 'fulfilled' && result.value) {
        const url = result.value;
        this.cacheServerUrl(url);
        return { serverUrl: url, fromCache: false };
      }
    }

    throw new Error('No server found via any method');
  }

  // ── Warm Cache ────────────────────────────────────────────

  /**
   * Pre-populate frequently accessed data during idle time.
   * Call after the app is interactive and essential data is loaded.
   */
  async warmCache(tasks: Array<{ name: string; task: () => Promise<void> }>): Promise<void> {
    if (this._destroyed) return;

    for (const { name, task } of tasks) {
      if (this._destroyed) break;

      try {
        // Use requestIdleCallback if available, else setTimeout
        await new Promise<void>((resolve) => {
          const run = async () => {
            try { await task(); } catch {}
            resolve();
          };

          if ('requestIdleCallback' in window) {
            (window as any).requestIdleCallback(run, { timeout: 5_000 });
          } else {
            setTimeout(run, 100);
          }
        });
      } catch {}
    }
  }

  // ── Event Subscription ────────────────────────────────────

  onTiming(cb: TimingCallback): () => void {
    this._listeners.push(cb);
    return () => {
      this._listeners = this._listeners.filter(l => l !== cb);
    };
  }

  // ── Get Timing ────────────────────────────────────────────

  getTiming(): StartupTiming {
    return {
      totalMs: this._timing.totalMs ?? 0,
      splashMs: this._timing.splashMs ?? 0,
      backendCheckMs: this._timing.backendCheckMs ?? 0,
      discoveryMs: this._timing.discoveryMs ?? 0,
      sessionRestoreMs: this._timing.sessionRestoreMs ?? 0,
      deferredInitMs: this._timing.deferredInitMs ?? 0,
      usedCachedServer: this._timing.usedCachedServer ?? false,
      discoverySkipped: this._timing.discoverySkipped ?? false,
      deferredServiceCount: this._deferredQueue.length,
      interactiveAt: this._timing.interactiveAt ?? 0,
    };
  }

  /**
   * Get the previous session's startup timing (for comparison).
   */
  getPreviousTiming(): StartupTiming | null {
    try {
      const stored = localStorage.getItem(CACHE_KEY_STARTUP_TIMING);
      return stored ? JSON.parse(stored) : null;
    } catch {
      return null;
    }
  }

  // ── Internal ──────────────────────────────────────────────

  private async _runDeferredQueue(): Promise<void> {
    if (this._destroyed) return;

    this.markPhaseStart('deferredInit');
    let count = 0;

    for (const { name, init } of this._deferredQueue) {
      if (this._destroyed) break;
      try {
        await init();
        count++;
      } catch (err) {
        console.warn(`[StartupOptimizer] Deferred init failed: ${name}`, err);
      }
    }

    this._timing.deferredServiceCount = count;
    this.markPhaseEnd('deferredInit');
    this._deferredQueue = [];
  }

  private _runIdlePreloads(): void {
    if (this._destroyed) return;

    const hints = this._preloadHints
      .filter(h => !h.loaded)
      .sort((a, b) => {
        const order = { critical: 0, normal: 1, low: 2 };
        return order[a.priority] - order[b.priority];
      });

    for (const hint of hints) {
      if (this._destroyed) break;

      const load = () => {
        hint.loaded = true;
        // Trigger dynamic import to preload the module into cache
        try {
          // This works with Vite's code splitting
          import(/* @vite-ignore */ hint.module).catch(() => {});
        } catch {}
      };

      if ('requestIdleCallback' in window) {
        (window as any).requestIdleCallback(load, { timeout: 10_000 });
      } else {
        setTimeout(load, 500);
      }
    }
  }

  private _saveTiming(): void {
    try {
      localStorage.setItem(CACHE_KEY_STARTUP_TIMING, JSON.stringify(this.getTiming()));
    } catch {}
  }

  private _clearServerCache(): void {
    try {
      localStorage.removeItem(CACHE_KEY_SERVER_URL);
      localStorage.removeItem(CACHE_KEY_SERVER_HEALTH_TS);
    } catch {}
  }
}

// ── Singleton ───────────────────────────────────────────────

export const startupOptimizer = new StartupOptimizer();
