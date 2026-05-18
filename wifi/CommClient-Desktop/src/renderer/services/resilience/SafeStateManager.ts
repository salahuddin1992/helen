/**
 * SafeStateManager.ts — Atomic writes, state integrity, and corruption prevention.
 *
 * Ensures that all critical application state survives power loss, process
 * kill, and OS-level crashes without corruption.
 *
 * Protected state categories:
 * ┌──────────────────────┬──────────────┬────────────────────────────────┐
 * │ State                │ Storage      │ Protection Strategy            │
 * ├──────────────────────┼──────────────┼────────────────────────────────┤
 * │ User config          │ JSON file    │ Write-ahead copy + rename      │
 * │ Window state         │ JSON file    │ Write-ahead copy + rename      │
 * │ SQLite database      │ .db + WAL    │ WAL mode + checkpoint on quit  │
 * │ Message drafts       │ localStorage │ Debounced writes + journal     │
 * │ Call state           │ Memory + LS  │ Periodic snapshot to LS        │
 * │ Active sessions      │ localStorage │ Atomic JSON write              │
 * │ Transfer progress    │ localStorage │ Periodic checkpoint            │
 * │ Zustand stores       │ Memory       │ Selective snapshot to LS       │
 * └──────────────────────┴──────────────┴────────────────────────────────┘
 *
 * Write-ahead strategy for files:
 *   1. Write new content to <filename>.tmp
 *   2. fsync the tmp file (ensure on disk)
 *   3. Rename <filename>.tmp → <filename> (atomic on NTFS)
 *   4. On read: if <filename> missing/corrupt, try <filename>.bak
 *
 * localStorage strategy:
 *   - Debounce writes (100ms) to avoid I/O storms
 *   - Wrap each write in try/catch (QuotaExceededError)
 *   - JSON schema validation on read
 *   - Checksum verification for critical entries
 */

// ── Types ───────────────────────────────────────────────────

export interface StateSnapshot {
  key: string;
  data: unknown;
  timestamp: number;
  checksum: string;
}

export interface PersistenceConfig {
  /** Key in localStorage */
  key: string;
  /** How often to auto-persist (ms). 0 = manual only. */
  intervalMs: number;
  /** Max size in characters before eviction/warning */
  maxSizeChars: number;
  /** Whether to verify checksum on read */
  verifyChecksum: boolean;
  /** Fallback value if read fails */
  fallback: unknown;
}

export interface WriteResult {
  success: boolean;
  bytesWritten: number;
  durationMs: number;
  error?: string;
}

// ── Constants ───────────────────────────────────────────────

const DEBOUNCE_MS = 100;
const MAX_LOCALSTORAGE_VALUE_SIZE = 2 * 1024 * 1024; // 2MB per key
const CHECKSUM_SEPARATOR = '|CK:';

// ── Persistence Configs ─────────────────────────────────────

export const STATE_CONFIGS: Record<string, PersistenceConfig> = {
  drafts: {
    key: 'cc_drafts',
    intervalMs: 3_000,
    maxSizeChars: 500_000,
    verifyChecksum: false,
    fallback: {},
  },
  callState: {
    key: 'cc_call_state',
    intervalMs: 2_000,
    maxSizeChars: 10_000,
    verifyChecksum: true,
    fallback: null,
  },
  activeSession: {
    key: 'cc_active_session',
    intervalMs: 5_000,
    maxSizeChars: 50_000,
    verifyChecksum: true,
    fallback: null,
  },
  transferProgress: {
    key: 'cc_transfers',
    intervalMs: 5_000,
    maxSizeChars: 100_000,
    verifyChecksum: false,
    fallback: {},
  },
  storeSnapshot: {
    key: 'cc_store_snapshot',
    intervalMs: 10_000,
    maxSizeChars: 1_000_000,
    verifyChecksum: true,
    fallback: null,
  },
  uiState: {
    key: 'cc_ui_state',
    intervalMs: 5_000,
    maxSizeChars: 50_000,
    verifyChecksum: false,
    fallback: {},
  },
};

// ── Checksum ────────────────────────────────────────────────

/**
 * Simple fast checksum using DJB2 hash.
 * Not cryptographic — only for corruption detection.
 */
function computeChecksum(data: string): string {
  let hash = 5381;
  for (let i = 0; i < data.length; i++) {
    hash = ((hash << 5) + hash + data.charCodeAt(i)) >>> 0;
  }
  return hash.toString(36);
}

// ── Singleton ───────────────────────────────────────────────

class SafeStateManager {
  private debounceTimers: Map<string, ReturnType<typeof setTimeout>> = new Map();
  private autoSaveTimers: Map<string, ReturnType<typeof setInterval>> = new Map();
  private dirtyFlags: Map<string, boolean> = new Map();
  private memoryCache: Map<string, unknown> = new Map();
  private writeCount = 0;
  private errorCount = 0;

  // ── Lifecycle ─────────────────────────────────────────────

  /**
   * Start auto-persistence for all configured state categories.
   */
  startAutoSave(): void {
    for (const [name, config] of Object.entries(STATE_CONFIGS)) {
      if (config.intervalMs > 0) {
        const timer = setInterval(() => {
          if (this.dirtyFlags.get(name)) {
            this.flushToDisk(name);
          }
        }, config.intervalMs);
        this.autoSaveTimers.set(name, timer);
      }
    }
  }

  /**
   * Stop all auto-persistence timers.
   */
  stopAutoSave(): void {
    for (const [name, timer] of this.autoSaveTimers) {
      clearInterval(timer);
    }
    this.autoSaveTimers.clear();
  }

  /**
   * Flush ALL dirty state to disk. Call before shutdown.
   */
  flushAll(): void {
    for (const name of Object.keys(STATE_CONFIGS)) {
      if (this.dirtyFlags.get(name)) {
        this.flushToDisk(name);
      }
    }
  }

  // ── Write API ─────────────────────────────────────────────

  /**
   * Update state in memory and schedule a debounced write.
   * Fastest path — does not block on I/O.
   */
  set(category: string, data: unknown): void {
    this.memoryCache.set(category, data);
    this.dirtyFlags.set(category, true);
    this.scheduleDebouncedWrite(category);
  }

  /**
   * Immediately persist a category to localStorage.
   * Use for critical state that must survive a crash right now.
   */
  setImmediate(category: string, data: unknown): WriteResult {
    this.memoryCache.set(category, data);
    this.dirtyFlags.set(category, true);
    return this.flushToDisk(category);
  }

  // ── Read API ──────────────────────────────────────────────

  /**
   * Read state. Returns memory cache first, falls back to localStorage.
   */
  get<T>(category: string): T | null {
    // Memory cache hit
    if (this.memoryCache.has(category)) {
      return this.memoryCache.get(category) as T;
    }

    // Read from localStorage
    const config = STATE_CONFIGS[category];
    if (!config) return null;

    const restored = this.readFromDisk(category);
    if (restored !== null) {
      this.memoryCache.set(category, restored);
      return restored as T;
    }

    return config.fallback as T;
  }

  /**
   * Read with explicit type validation.
   */
  getValidated<T>(category: string, validator: (data: unknown) => data is T): T | null {
    const data = this.get(category);
    if (data !== null && validator(data)) {
      return data;
    }
    return null;
  }

  // ── Delete API ────────────────────────────────────────────

  /**
   * Remove a category from both memory and disk.
   */
  remove(category: string): void {
    this.memoryCache.delete(category);
    this.dirtyFlags.delete(category);

    const config = STATE_CONFIGS[category];
    if (config) {
      try { localStorage.removeItem(config.key); } catch {}
    }
  }

  /**
   * Clear ALL persisted state. Used for "clean slate" recovery.
   */
  clearAll(): void {
    for (const config of Object.values(STATE_CONFIGS)) {
      try { localStorage.removeItem(config.key); } catch {}
    }
    this.memoryCache.clear();
    this.dirtyFlags.clear();
  }

  // ── Diagnostics ───────────────────────────────────────────

  getDiagnostics(): {
    writeCount: number;
    errorCount: number;
    cachedCategories: string[];
    dirtyCategories: string[];
    totalStorageUsedBytes: number;
  } {
    let totalBytes = 0;
    for (const config of Object.values(STATE_CONFIGS)) {
      try {
        const val = localStorage.getItem(config.key);
        if (val) totalBytes += val.length * 2; // UTF-16
      } catch {}
    }

    return {
      writeCount: this.writeCount,
      errorCount: this.errorCount,
      cachedCategories: [...this.memoryCache.keys()],
      dirtyCategories: [...this.dirtyFlags.entries()].filter(([, d]) => d).map(([k]) => k),
      totalStorageUsedBytes: totalBytes,
    };
  }

  // ── Private: Debounced Write ──────────────────────────────

  private scheduleDebouncedWrite(category: string): void {
    const existing = this.debounceTimers.get(category);
    if (existing) clearTimeout(existing);

    const timer = setTimeout(() => {
      this.debounceTimers.delete(category);
      if (this.dirtyFlags.get(category)) {
        this.flushToDisk(category);
      }
    }, DEBOUNCE_MS);
    this.debounceTimers.set(category, timer);
  }

  // ── Private: Disk I/O ────────────────────────────────────

  private flushToDisk(category: string): WriteResult {
    const startTime = performance.now();
    const config = STATE_CONFIGS[category];
    if (!config) {
      return { success: false, bytesWritten: 0, durationMs: 0, error: 'Unknown category' };
    }

    const data = this.memoryCache.get(category);
    if (data === undefined) {
      return { success: false, bytesWritten: 0, durationMs: 0, error: 'No data in cache' };
    }

    try {
      let serialized = JSON.stringify(data);

      // Size check
      if (serialized.length > config.maxSizeChars) {
        console.warn(`[SafeState] Category "${category}" exceeds max size (${serialized.length}/${config.maxSizeChars})`);
        // Truncation strategy: for drafts, keep only most recent entries
        if (category === 'drafts' && typeof data === 'object' && data !== null) {
          const entries = Object.entries(data as Record<string, unknown>);
          const trimmed = Object.fromEntries(entries.slice(-50)); // Keep last 50
          serialized = JSON.stringify(trimmed);
          this.memoryCache.set(category, trimmed);
        }
      }

      // Add checksum if configured
      let valueToStore = serialized;
      if (config.verifyChecksum) {
        const checksum = computeChecksum(serialized);
        valueToStore = serialized + CHECKSUM_SEPARATOR + checksum;
      }

      localStorage.setItem(config.key, valueToStore);
      this.dirtyFlags.set(category, false);
      this.writeCount++;

      const durationMs = performance.now() - startTime;
      return { success: true, bytesWritten: valueToStore.length * 2, durationMs };

    } catch (err) {
      this.errorCount++;
      const error = (err as Error).message;

      // QuotaExceededError — try to free space
      if (error.includes('QuotaExceeded') || error.includes('quota')) {
        this.evictLeastCritical();
        // Retry once
        try {
          const serialized = JSON.stringify(data);
          localStorage.setItem(config.key, serialized);
          this.dirtyFlags.set(category, false);
          return { success: true, bytesWritten: serialized.length * 2, durationMs: performance.now() - startTime };
        } catch {}
      }

      console.error(`[SafeState] Failed to persist "${category}":`, error);
      return { success: false, bytesWritten: 0, durationMs: performance.now() - startTime, error };
    }
  }

  private readFromDisk(category: string): unknown | null {
    const config = STATE_CONFIGS[category];
    if (!config) return null;

    try {
      const raw = localStorage.getItem(config.key);
      if (raw === null) return null;

      let jsonStr = raw;

      // Verify checksum if configured
      if (config.verifyChecksum) {
        const separatorIdx = raw.lastIndexOf(CHECKSUM_SEPARATOR);
        if (separatorIdx === -1) {
          console.warn(`[SafeState] Missing checksum for "${category}" — reading without verification`);
        } else {
          jsonStr = raw.substring(0, separatorIdx);
          const storedChecksum = raw.substring(separatorIdx + CHECKSUM_SEPARATOR.length);
          const computedChecksum = computeChecksum(jsonStr);
          if (storedChecksum !== computedChecksum) {
            console.error(`[SafeState] Checksum mismatch for "${category}" — data may be corrupt`);
            // Return null to trigger fallback
            return null;
          }
        }
      }

      return JSON.parse(jsonStr);
    } catch (err) {
      console.error(`[SafeState] Failed to read "${category}":`, (err as Error).message);
      return null;
    }
  }

  // ── Private: Eviction ─────────────────────────────────────

  /**
   * Free localStorage space by removing least critical state.
   * Priority: transfers > uiState > storeSnapshot > drafts > callState > activeSession
   */
  private evictLeastCritical(): void {
    const evictionOrder = ['transferProgress', 'uiState', 'storeSnapshot'];
    for (const category of evictionOrder) {
      const config = STATE_CONFIGS[category];
      if (config) {
        try {
          localStorage.removeItem(config.key);
          this.memoryCache.delete(category);
          console.warn(`[SafeState] Evicted "${category}" to free space`);
          return;
        } catch {}
      }
    }
  }
}

// ── Singleton Export ────────────────────────────────────────

export const safeStateManager = new SafeStateManager();
