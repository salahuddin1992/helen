/**
 * DiagnosticsLogger.ts — Phase 14: Structured Diagnostics Logging Engine
 *
 * Builds a production-grade diagnostics layer ON TOP of the existing AppLogger
 * (which remains untouched). This module adds:
 *
 *   1. **Log Categories** — 12 semantic categories so every log entry can be
 *      filtered by domain (startup, auth, messaging, calls, screenshare,
 *      network, media, database, performance, ui, system, resilience).
 *
 *   2. **Log Levels** — 5-level scale (TRACE → DEBUG → INFO → WARN → ERROR)
 *      with separate thresholds for console vs. persistence.
 *
 *   3. **File Persistence via IPC** — Renderer sends log batches to the main
 *      process through `window.electronAPI.writeDiagnosticLog()`. Main process
 *      writes to %APPDATA%/CommClient/logs/renderer-YYYY-MM-DD.log with daily
 *      rotation and configurable retention.
 *
 *   4. **Dual-Ring Buffer** — A "normal" ring buffer (1 000 entries, INFO+)
 *      and a "debug" ring buffer (2 000 entries, TRACE+). The debug buffer
 *      is only active when diagnostics debug mode is on.
 *
 *   5. **Privacy Sanitization** — Strips tokens, passwords, IPs from log
 *      payloads before persistence.
 *
 *   6. **Batched I/O** — Accumulates entries and flushes every 3 seconds
 *      (or immediately on ERROR / shutdown) to avoid I/O storms.
 *
 *   7. **Session Tagging** — Every log entry carries a sessionId so logs
 *      from different app runs can be correlated.
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │                    DiagnosticsLogger Data Flow                       │
 * │                                                                      │
 * │  Component ──log()──► DiagnosticsLogger                              │
 * │                          │                                           │
 * │                    ┌─────┴──────┐                                    │
 * │                    ▼            ▼                                    │
 * │              Ring Buffer    Batch Queue                              │
 * │              (in-memory)    (pending flush)                          │
 * │                    │            │                                    │
 * │                    │      ┌─────┴───────────┐                       │
 * │                    │      ▼                  ▼                       │
 * │                    │  Console Output    IPC → Main Process           │
 * │                    │                     │                           │
 * │                    │                     ▼                           │
 * │                    │              File Writer                        │
 * │                    │         renderer-YYYY-MM-DD.log                 │
 * │                    │                                                 │
 * │                    └──► Export / Diagnostics Collector                │
 * └──────────────────────────────────────────────────────────────────────┘
 */

import { AppLogger, type LogLevel as AppLogLevel } from '../AppLogger';

// ── Log Level ──────────────────────────────────────────────────

export type DiagLogLevel = 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';

const LOG_LEVEL_VALUE: Record<DiagLogLevel, number> = {
  TRACE: 0,
  DEBUG: 1,
  INFO: 2,
  WARN: 3,
  ERROR: 4,
};

// ── Log Categories ─────────────────────────────────────────────

export type LogCategory =
  | 'startup'
  | 'auth'
  | 'messaging'
  | 'calls'
  | 'screenshare'
  | 'network'
  | 'media'
  | 'database'
  | 'performance'
  | 'ui'
  | 'system'
  | 'resilience';

/** Human-readable labels for each category */
export const LOG_CATEGORY_LABELS: Record<LogCategory, { en: string; ar: string }> = {
  startup:     { en: 'Startup',        ar: 'بدء التشغيل' },
  auth:        { en: 'Authentication', ar: 'المصادقة' },
  messaging:   { en: 'Messaging',      ar: 'الرسائل' },
  calls:       { en: 'Calls',          ar: 'المكالمات' },
  screenshare: { en: 'Screen Share',   ar: 'مشاركة الشاشة' },
  network:     { en: 'Network',        ar: 'الشبكة' },
  media:       { en: 'Media',          ar: 'الوسائط' },
  database:    { en: 'Database',       ar: 'قاعدة البيانات' },
  performance: { en: 'Performance',    ar: 'الأداء' },
  ui:          { en: 'UI',             ar: 'واجهة المستخدم' },
  system:      { en: 'System',         ar: 'النظام' },
  resilience:  { en: 'Resilience',     ar: 'المرونة' },
};

// ── Log Entry ──────────────────────────────────────────────────

export interface DiagLogEntry {
  /** ISO timestamp */
  ts: string;
  /** Monotonic ms since session start */
  elapsed: number;
  /** Session identifier (unique per app launch) */
  sessionId: string;
  /** Log level */
  level: DiagLogLevel;
  /** Semantic category */
  category: LogCategory;
  /** Source module or component */
  source: string;
  /** Human-readable message */
  message: string;
  /** Structured payload (sanitized before persistence) */
  data?: Record<string, unknown>;
  /** Error stack trace if applicable */
  stack?: string;
  /** Duration in ms (for timed operations) */
  durationMs?: number;
}

// ── Configuration ──────────────────────────────────────────────

export interface DiagLogConfig {
  /** Min level for console output */
  consoleLevel: DiagLogLevel;
  /** Min level for file persistence */
  persistLevel: DiagLogLevel;
  /** Min level for normal ring buffer */
  bufferLevel: DiagLogLevel;
  /** Enable debug ring buffer (TRACE+) */
  debugBufferEnabled: boolean;
  /** Normal buffer max entries */
  normalBufferSize: number;
  /** Debug buffer max entries */
  debugBufferSize: number;
  /** Batch flush interval (ms) */
  flushIntervalMs: number;
  /** Max batch size before forced flush */
  maxBatchSize: number;
  /** Enable file persistence via IPC */
  filePersistenceEnabled: boolean;
  /** Enable console output */
  consoleEnabled: boolean;
  /** Category filter (null = all enabled) */
  enabledCategories: Set<LogCategory> | null;
  /** Muted categories (overrides enabledCategories) */
  mutedCategories: Set<LogCategory>;
}

const DEFAULT_CONFIG: DiagLogConfig = {
  consoleLevel: 'INFO',
  persistLevel: 'INFO',
  bufferLevel: 'INFO',
  debugBufferEnabled: false,
  normalBufferSize: 1_000,
  debugBufferSize: 2_000,
  flushIntervalMs: 3_000,
  maxBatchSize: 100,
  filePersistenceEnabled: true,
  consoleEnabled: true,
  enabledCategories: null,
  mutedCategories: new Set(),
};

// ── Privacy Sanitizer ──────────────────────────────────────────

/** Patterns that indicate sensitive data — replaced with [REDACTED] */
const SENSITIVE_PATTERNS: Array<{ pattern: RegExp; replacement: string }> = [
  // Auth tokens / JWTs
  { pattern: /eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g, replacement: '[JWT_REDACTED]' },
  // Bearer tokens
  { pattern: /(bearer\s+)[^\s"']+/gi, replacement: '$1[TOKEN_REDACTED]' },
  // Passwords in JSON
  { pattern: /("password"\s*:\s*")[^"]+"/gi, replacement: '$1[REDACTED]"' },
  // API keys
  { pattern: /(api[_-]?key["\s:=]+)[^\s"',}]+/gi, replacement: '$1[KEY_REDACTED]' },
  // Session tokens
  { pattern: /(session[_-]?token["\s:=]+)[^\s"',}]+/gi, replacement: '$1[TOKEN_REDACTED]' },
  // IPv4 addresses (preserve localhost/LAN prefix, redact last octet for privacy)
  { pattern: /\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.)\d{1,3}\b/g, replacement: '$1***' },
  // Email addresses
  { pattern: /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g, replacement: '[EMAIL_REDACTED]' },
];

function sanitizeValue(value: unknown): unknown {
  if (typeof value === 'string') {
    let sanitized = value;
    for (const { pattern, replacement } of SENSITIVE_PATTERNS) {
      // Reset regex lastIndex for global patterns
      pattern.lastIndex = 0;
      sanitized = sanitized.replace(pattern, replacement);
    }
    return sanitized;
  }
  if (Array.isArray(value)) {
    return value.map(sanitizeValue);
  }
  if (value !== null && typeof value === 'object') {
    return sanitizeObject(value as Record<string, unknown>);
  }
  return value;
}

function sanitizeObject(obj: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, val] of Object.entries(obj)) {
    // Skip keys that are obviously sensitive
    const lk = key.toLowerCase();
    if (lk.includes('password') || lk.includes('secret') || lk.includes('token') || lk.includes('credential')) {
      result[key] = '[REDACTED]';
    } else {
      result[key] = sanitizeValue(val);
    }
  }
  return result;
}

// ── Ring Buffer ────────────────────────────────────────────────

class RingBuffer<T> {
  private readonly _items: (T | null)[];
  private _writeIdx = 0;
  private _count = 0;

  constructor(private readonly _capacity: number) {
    this._items = new Array(_capacity).fill(null);
  }

  push(item: T): void {
    this._items[this._writeIdx] = item;
    this._writeIdx = (this._writeIdx + 1) % this._capacity;
    if (this._count < this._capacity) this._count++;
  }

  /** Read all entries in chronological order */
  toArray(): T[] {
    if (this._count === 0) return [];
    const result: T[] = [];
    const start = this._count < this._capacity ? 0 : this._writeIdx;
    for (let i = 0; i < this._count; i++) {
      const idx = (start + i) % this._capacity;
      const item = this._items[idx];
      if (item !== null) result.push(item);
    }
    return result;
  }

  /** Read last N entries */
  last(n: number): T[] {
    const all = this.toArray();
    return all.slice(-n);
  }

  get size(): number { return this._count; }
  get capacity(): number { return this._capacity; }

  clear(): void {
    this._items.fill(null);
    this._writeIdx = 0;
    this._count = 0;
  }
}

// ── IPC Bridge Types ───────────────────────────────────────────

/** Expected shape of the IPC bridge exposed by preload */
interface DiagnosticsIPC {
  writeDiagnosticLog(entries: string[]): Promise<void>;
}

function getIPC(): DiagnosticsIPC | null {
  try {
    const api = (window as any).electronAPI;
    if (api && typeof api.writeDiagnosticLog === 'function') {
      return api as DiagnosticsIPC;
    }
  } catch { /* not in Electron context */ }
  return null;
}

// ── Session ID Generator ───────────────────────────────────────

function generateSessionId(): string {
  const ts = Date.now().toString(36);
  const rand = Math.random().toString(36).substring(2, 8);
  return `s_${ts}_${rand}`;
}

// ── Category Logger (scoped convenience interface) ─────────────

export interface CategoryLogger {
  trace: (message: string, data?: Record<string, unknown>) => void;
  debug: (message: string, data?: Record<string, unknown>) => void;
  info:  (message: string, data?: Record<string, unknown>) => void;
  warn:  (message: string, data?: Record<string, unknown>) => void;
  error: (message: string, error?: Error | Record<string, unknown>) => void;
  time:  (label: string) => { end: (data?: Record<string, unknown>) => void };
}

// ── Singleton ──────────────────────────────────────────────────

class DiagnosticsLoggerEngine {
  private _config: DiagLogConfig = { ...DEFAULT_CONFIG, mutedCategories: new Set() };
  private readonly _sessionId: string;
  private readonly _sessionStartMs: number;

  // Buffers
  private readonly _normalBuffer: RingBuffer<DiagLogEntry>;
  private _debugBuffer: RingBuffer<DiagLogEntry> | null = null;

  // Batch queue for file persistence
  private _batchQueue: string[] = [];
  private _flushTimer: ReturnType<typeof setInterval> | null = null;

  // Stats
  private _totalLogged = 0;
  private _totalFlushed = 0;
  private _totalDropped = 0;
  private readonly _categoryCounters: Record<LogCategory, Record<DiagLogLevel, number>>;

  // Internal AppLogger instance for meta-logging
  private readonly _metaLog = AppLogger.create('Diagnostics');

  constructor() {
    this._sessionId = generateSessionId();
    this._sessionStartMs = Date.now();
    this._normalBuffer = new RingBuffer<DiagLogEntry>(DEFAULT_CONFIG.normalBufferSize);

    // Initialize category counters
    const categories: LogCategory[] = [
      'startup', 'auth', 'messaging', 'calls', 'screenshare',
      'network', 'media', 'database', 'performance', 'ui', 'system', 'resilience',
    ];
    this._categoryCounters = {} as any;
    for (const cat of categories) {
      this._categoryCounters[cat] = { TRACE: 0, DEBUG: 0, INFO: 0, WARN: 0, ERROR: 0 };
    }
  }

  // ── Lifecycle ──────────────────────────────────────────

  /**
   * Start the diagnostics logger. Call once at app startup.
   */
  start(configOverrides?: Partial<DiagLogConfig>): void {
    if (configOverrides) {
      this._config = {
        ...this._config,
        ...configOverrides,
        mutedCategories: configOverrides.mutedCategories ?? this._config.mutedCategories,
        enabledCategories: configOverrides.enabledCategories ?? this._config.enabledCategories,
      };
    }

    // Init debug buffer if enabled
    if (this._config.debugBufferEnabled && !this._debugBuffer) {
      this._debugBuffer = new RingBuffer<DiagLogEntry>(this._config.debugBufferSize);
    }

    // Start batch flush timer
    if (this._config.filePersistenceEnabled && !this._flushTimer) {
      this._flushTimer = setInterval(() => this._flushBatch(), this._config.flushIntervalMs);
    }

    this._metaLog.info('DiagnosticsLogger started', {
      sessionId: this._sessionId,
      consoleLevel: this._config.consoleLevel,
      persistLevel: this._config.persistLevel,
      debugBuffer: this._config.debugBufferEnabled,
    });
  }

  /**
   * Flush pending logs and stop. Call before app shutdown.
   */
  async shutdown(): Promise<void> {
    if (this._flushTimer) {
      clearInterval(this._flushTimer);
      this._flushTimer = null;
    }
    await this._flushBatch();
    this._metaLog.info('DiagnosticsLogger shut down', {
      totalLogged: this._totalLogged,
      totalFlushed: this._totalFlushed,
      totalDropped: this._totalDropped,
    });
  }

  // ── Core Logging ───────────────────────────────────────

  /**
   * Write a log entry.
   */
  log(
    level: DiagLogLevel,
    category: LogCategory,
    source: string,
    message: string,
    data?: Record<string, unknown>,
    error?: Error,
  ): void {
    // Category filter
    if (this._config.mutedCategories.has(category)) return;
    if (this._config.enabledCategories && !this._config.enabledCategories.has(category)) return;

    const now = new Date();
    const entry: DiagLogEntry = {
      ts: now.toISOString(),
      elapsed: Date.now() - this._sessionStartMs,
      sessionId: this._sessionId,
      level,
      category,
      source,
      message,
      data: data ? sanitizeObject(data) : undefined,
      stack: error?.stack ? sanitizeValue(error.stack) as string : undefined,
    };

    this._totalLogged++;
    this._categoryCounters[category][level]++;

    // Normal buffer (INFO+)
    if (LOG_LEVEL_VALUE[level] >= LOG_LEVEL_VALUE[this._config.bufferLevel]) {
      this._normalBuffer.push(entry);
    }

    // Debug buffer (TRACE+ when enabled)
    if (this._debugBuffer) {
      this._debugBuffer.push(entry);
    }

    // Console output
    if (this._config.consoleEnabled && LOG_LEVEL_VALUE[level] >= LOG_LEVEL_VALUE[this._config.consoleLevel]) {
      this._consoleOutput(entry);
    }

    // File persistence queue
    if (this._config.filePersistenceEnabled && LOG_LEVEL_VALUE[level] >= LOG_LEVEL_VALUE[this._config.persistLevel]) {
      this._enqueuePersist(entry);
    }
  }

  // ── Category Logger Factory ────────────────────────────

  /**
   * Create a scoped logger for a specific category and source module.
   * This is the primary interface for components to use.
   *
   * @example
   *   const log = diagnosticsLogger.createLogger('calls', 'CallEngine');
   *   log.info('Call started', { callId: '123' });
   *   const timer = log.time('ice-negotiation');
   *   // ... work ...
   *   timer.end({ candidates: 5 });
   */
  createLogger(category: LogCategory, source: string): CategoryLogger {
    return {
      trace: (message, data?) => this.log('TRACE', category, source, message, data),
      debug: (message, data?) => this.log('DEBUG', category, source, message, data),
      info:  (message, data?) => this.log('INFO', category, source, message, data),
      warn:  (message, data?) => this.log('WARN', category, source, message, data),
      error: (message, errorOrData?) => {
        if (errorOrData instanceof Error) {
          this.log('ERROR', category, source, message, { error: errorOrData.message }, errorOrData);
        } else {
          this.log('ERROR', category, source, message, errorOrData);
        }
      },
      time: (label: string) => {
        const start = performance.now();
        return {
          end: (data?: Record<string, unknown>) => {
            const durationMs = Math.round(performance.now() - start);
            this.log('INFO', category, source, `${label} completed`, {
              ...data,
              durationMs,
            });
          },
        };
      },
    };
  }

  // ── Configuration at Runtime ───────────────────────────

  /**
   * Update config at runtime (e.g., user toggles debug mode).
   */
  configure(overrides: Partial<DiagLogConfig>): void {
    this._config = {
      ...this._config,
      ...overrides,
      mutedCategories: overrides.mutedCategories ?? this._config.mutedCategories,
      enabledCategories: overrides.enabledCategories ?? this._config.enabledCategories,
    };

    // Toggle debug buffer
    if (this._config.debugBufferEnabled && !this._debugBuffer) {
      this._debugBuffer = new RingBuffer<DiagLogEntry>(this._config.debugBufferSize);
    } else if (!this._config.debugBufferEnabled && this._debugBuffer) {
      this._debugBuffer = null;
    }

    // Toggle flush timer
    if (this._config.filePersistenceEnabled && !this._flushTimer) {
      this._flushTimer = setInterval(() => this._flushBatch(), this._config.flushIntervalMs);
    } else if (!this._config.filePersistenceEnabled && this._flushTimer) {
      clearInterval(this._flushTimer);
      this._flushTimer = null;
    }
  }

  /**
   * Enable debug mode (TRACE logging + debug buffer).
   */
  enableDebugMode(): void {
    this.configure({
      consoleLevel: 'TRACE',
      persistLevel: 'DEBUG',
      debugBufferEnabled: true,
    });
    this._metaLog.info('Debug mode enabled');
  }

  /**
   * Disable debug mode (back to INFO defaults).
   */
  disableDebugMode(): void {
    this.configure({
      consoleLevel: 'INFO',
      persistLevel: 'INFO',
      debugBufferEnabled: false,
    });
    this._metaLog.info('Debug mode disabled');
  }

  /**
   * Mute a category (stop logging it).
   */
  muteCategory(category: LogCategory): void {
    this._config.mutedCategories.add(category);
  }

  /**
   * Unmute a category.
   */
  unmuteCategory(category: LogCategory): void {
    this._config.mutedCategories.delete(category);
  }

  // ── Query & Export ─────────────────────────────────────

  /**
   * Get recent log entries from the normal buffer.
   */
  getRecentLogs(count?: number): DiagLogEntry[] {
    return count ? this._normalBuffer.last(count) : this._normalBuffer.toArray();
  }

  /**
   * Get debug-level log entries (only available when debug buffer is active).
   */
  getDebugLogs(count?: number): DiagLogEntry[] {
    if (!this._debugBuffer) return [];
    return count ? this._debugBuffer.last(count) : this._debugBuffer.toArray();
  }

  /**
   * Get logs filtered by category.
   */
  getLogsByCategory(category: LogCategory, count?: number): DiagLogEntry[] {
    const all = this._normalBuffer.toArray().filter(e => e.category === category);
    return count ? all.slice(-count) : all;
  }

  /**
   * Get logs filtered by level (this level and above).
   */
  getLogsByLevel(minLevel: DiagLogLevel): DiagLogEntry[] {
    const minVal = LOG_LEVEL_VALUE[minLevel];
    return this._normalBuffer.toArray().filter(e => LOG_LEVEL_VALUE[e.level] >= minVal);
  }

  /**
   * Export all buffered logs as a formatted text string.
   */
  exportAsText(): string {
    const entries = this._normalBuffer.toArray();
    const header = [
      `=== CommClient Diagnostics Log ===`,
      `Session: ${this._sessionId}`,
      `Exported: ${new Date().toISOString()}`,
      `Entries: ${entries.length}`,
      `Uptime: ${Math.round((Date.now() - this._sessionStartMs) / 1000)}s`,
      `==================================`,
      '',
    ].join('\n');

    const lines = entries.map(e => {
      const base = `${e.ts} [${e.level.padEnd(5)}] [${e.category}] [${e.source}] ${e.message}`;
      const dataPart = e.data ? ` ${JSON.stringify(e.data)}` : '';
      const stackPart = e.stack ? `\n  STACK: ${e.stack}` : '';
      return base + dataPart + stackPart;
    });

    return header + lines.join('\n');
  }

  /**
   * Export all buffered logs as JSON (for DiagnosticsCollector).
   */
  exportAsJSON(): object {
    return {
      sessionId: this._sessionId,
      exportedAt: new Date().toISOString(),
      sessionStartedAt: new Date(this._sessionStartMs).toISOString(),
      uptimeMs: Date.now() - this._sessionStartMs,
      stats: this.getStats(),
      entries: this._normalBuffer.toArray(),
      debugEntries: this._debugBuffer ? this._debugBuffer.toArray() : [],
    };
  }

  /**
   * Get aggregate statistics.
   */
  getStats(): DiagnosticsStats {
    return {
      sessionId: this._sessionId,
      uptimeMs: Date.now() - this._sessionStartMs,
      totalLogged: this._totalLogged,
      totalFlushed: this._totalFlushed,
      totalDropped: this._totalDropped,
      normalBufferUsed: this._normalBuffer.size,
      normalBufferCapacity: this._normalBuffer.capacity,
      debugBufferUsed: this._debugBuffer?.size ?? 0,
      debugBufferCapacity: this._debugBuffer?.capacity ?? 0,
      batchQueueSize: this._batchQueue.length,
      categoryCounters: { ...this._categoryCounters },
      config: {
        consoleLevel: this._config.consoleLevel,
        persistLevel: this._config.persistLevel,
        debugBufferEnabled: this._config.debugBufferEnabled,
        filePersistenceEnabled: this._config.filePersistenceEnabled,
      },
    };
  }

  /**
   * Get current session ID.
   */
  getSessionId(): string {
    return this._sessionId;
  }

  /**
   * Clear all buffers and counters.
   */
  clearAll(): void {
    this._normalBuffer.clear();
    this._debugBuffer?.clear();
    this._batchQueue = [];
    this._totalLogged = 0;
    this._totalFlushed = 0;
    this._totalDropped = 0;
    for (const cat of Object.keys(this._categoryCounters) as LogCategory[]) {
      this._categoryCounters[cat] = { TRACE: 0, DEBUG: 0, INFO: 0, WARN: 0, ERROR: 0 };
    }
  }

  // ── Private: Console Output ────────────────────────────

  private _consoleOutput(entry: DiagLogEntry): void {
    const prefix = `${entry.ts.substring(11, 23)} [${entry.category}/${entry.source}]`;
    const msg = `${prefix} ${entry.message}`;

    const consoleFn =
      entry.level === 'ERROR' ? console.error
        : entry.level === 'WARN' ? console.warn
          : entry.level === 'TRACE' ? console.debug
            : entry.level === 'DEBUG' ? console.debug
              : console.log;

    if (entry.data) {
      consoleFn(msg, entry.data);
    } else {
      consoleFn(msg);
    }

    if (entry.stack) {
      console.debug(`  └─ Stack: ${entry.stack.split('\n')[0]}`);
    }
  }

  // ── Private: Batch Persistence ─────────────────────────

  private _enqueuePersist(entry: DiagLogEntry): void {
    const line = `${entry.ts}\t${entry.level}\t${entry.category}\t${entry.source}\t${entry.message}` +
      (entry.data ? `\t${JSON.stringify(entry.data)}` : '') +
      (entry.stack ? `\tSTACK:${entry.stack.split('\n').slice(0, 3).join(' | ')}` : '');

    this._batchQueue.push(line);

    // Force flush on ERROR or batch overflow
    if (entry.level === 'ERROR' || this._batchQueue.length >= this._config.maxBatchSize) {
      this._flushBatch().catch(() => {});
    }
  }

  private async _flushBatch(): Promise<void> {
    if (this._batchQueue.length === 0) return;

    const batch = this._batchQueue.splice(0);
    const ipc = getIPC();

    if (ipc) {
      try {
        await ipc.writeDiagnosticLog(batch);
        this._totalFlushed += batch.length;
      } catch (err) {
        // IPC failed — entries are lost
        this._totalDropped += batch.length;
        console.warn('[DiagnosticsLogger] IPC flush failed, dropped', batch.length, 'entries');
      }
    } else {
      // No IPC available — fall back to localStorage snapshot
      this._persistToLocalStorage(batch);
    }
  }

  /**
   * Fallback persistence when Electron IPC is unavailable.
   * Stores last 200 log lines in localStorage for diagnostic export.
   */
  private _persistToLocalStorage(batch: string[]): void {
    try {
      const KEY = 'commclient_diag_log';
      const MAX_LS_ENTRIES = 200;
      const existing = JSON.parse(localStorage.getItem(KEY) || '[]') as string[];
      const combined = [...existing, ...batch].slice(-MAX_LS_ENTRIES);
      localStorage.setItem(KEY, JSON.stringify(combined));
      this._totalFlushed += batch.length;
    } catch {
      this._totalDropped += batch.length;
    }
  }
}

// ── Stats Type ─────────────────────────────────────────────────

export interface DiagnosticsStats {
  sessionId: string;
  uptimeMs: number;
  totalLogged: number;
  totalFlushed: number;
  totalDropped: number;
  normalBufferUsed: number;
  normalBufferCapacity: number;
  debugBufferUsed: number;
  debugBufferCapacity: number;
  batchQueueSize: number;
  categoryCounters: Record<LogCategory, Record<DiagLogLevel, number>>;
  config: {
    consoleLevel: DiagLogLevel;
    persistLevel: DiagLogLevel;
    debugBufferEnabled: boolean;
    filePersistenceEnabled: boolean;
  };
}

// ── Singleton Export ───────────────────────────────────────────

export const diagnosticsLogger = new DiagnosticsLoggerEngine();
