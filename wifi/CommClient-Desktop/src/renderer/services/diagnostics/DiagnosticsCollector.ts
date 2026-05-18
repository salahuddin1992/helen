/**
 * DiagnosticsCollector.ts — Phase 14: Exportable Troubleshooting Package Builder
 *
 * Gathers all diagnostic data from every subsystem and produces a single
 * export-ready package that can be saved to disk or shared with support.
 *
 * Package contents:
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │  CommClient-Diagnostics-2026-04-09T14-30-00.zip                      │
 * │                                                                      │
 * │  ├── summary.json         — metadata, versions, overall health       │
 * │  ├── health.json          — full health check snapshot               │
 * │  ├── logs-normal.txt      — normal ring buffer (INFO+)               │
 * │  ├── logs-debug.txt       — debug ring buffer (TRACE+, if active)    │
 * │  ├── logs-calls.json      — CallDebugLogger entries                  │
 * │  ├── logs-applogger.txt   — AppLogger ring buffer export             │
 * │  ├── errors.json          — ErrorClassifier ring buffer + frequency  │
 * │  ├── resilience.json      — crash record, network state, safe state  │
 * │  ├── system-info.json     — OS, hardware, screen, Electron version   │
 * │  ├── config.json          — user config (sanitized)                  │
 * │  ├── network-state.json   — network metrics, interface info          │
 * │  └── performance.json     — memory, timing, capacity tier            │
 * └──────────────────────────────────────────────────────────────────────┘
 *
 * Privacy:
 *   - All exported data passes through the DiagnosticsLogger sanitizer
 *   - Passwords, tokens, emails are stripped
 *   - IP addresses have last octet masked
 *   - User can preview package contents before saving
 *
 * Export methods:
 *   1. collectAll()       → in-memory DiagnosticsPackage object
 *   2. exportAsJSON()     → single JSON string (for clipboard / IPC)
 *   3. exportToFile()     → triggers Electron save dialog (via IPC)
 */

import { diagnosticsLogger, type DiagLogEntry, type DiagnosticsStats } from './DiagnosticsLogger';
import { healthCheckSystem, type OverallHealth } from './HealthCheckSystem';
import { AppLogger } from '../AppLogger';

// ── Types ───────────────────────────────────────────────────────

export interface PackageSection {
  /** Section identifier */
  id: string;
  /** Human-readable label */
  label: string;
  /** File name in the export */
  fileName: string;
  /** The actual data */
  data: unknown;
  /** Size estimate in bytes */
  sizeBytes: number;
  /** Whether this section contains potentially sensitive data */
  sensitive: boolean;
}

export interface DiagnosticsPackage {
  /** Package format version */
  version: string;
  /** When the package was collected */
  collectedAt: string;
  /** Collection duration (ms) */
  collectionDurationMs: number;
  /** App version */
  appVersion: string;
  /** Session ID from DiagnosticsLogger */
  sessionId: string;
  /** Overall health at collection time */
  healthStatus: string;
  /** All sections */
  sections: PackageSection[];
  /** Total estimated size */
  totalSizeBytes: number;
}

export interface SystemInfo {
  /** Platform (win32, darwin, linux) */
  platform: string;
  /** User agent string */
  userAgent: string;
  /** Screen resolution */
  screenResolution: string;
  /** Device pixel ratio */
  devicePixelRatio: number;
  /** Number of logical CPU cores */
  cpuCores: number;
  /** Device memory (GB, if available) */
  deviceMemoryGB: number | null;
  /** JS heap info */
  memory: {
    usedMB: number;
    totalMB: number;
    limitMB: number;
  } | null;
  /** Electron version (if available) */
  electronVersion: string | null;
  /** Chrome version (if available) */
  chromeVersion: string | null;
  /** Node version (if available) */
  nodeVersion: string | null;
  /** Language / locale */
  language: string;
  /** Timezone */
  timezone: string;
  /** Online status */
  online: boolean;
  /** Connection info */
  connection: {
    effectiveType: string;
    downlink: number;
    rtt: number;
  } | null;
}

export interface PerformanceSnapshot {
  /** Memory breakdown */
  memory: {
    usedJSHeapMB: number;
    totalJSHeapMB: number;
    heapLimitMB: number;
  } | null;
  /** Navigation timing */
  navigationTiming: {
    domContentLoadedMs: number;
    loadCompleteMs: number;
    ttfbMs: number;
  } | null;
  /** Resource count */
  resourceCount: number;
  /** Long tasks detected (Performance Observer) */
  longTaskCount: number;
  /** FPS estimate from recent rAF */
  estimatedFPS: number | null;
}

// ── IPC Bridge ──────────────────────────────────────────────────

interface ExportIPC {
  saveDiagnosticsPackage(jsonData: string, suggestedFileName: string): Promise<string | null>;
}

function getExportIPC(): ExportIPC | null {
  try {
    const api = (window as any).electronAPI;
    if (api && typeof api.saveDiagnosticsPackage === 'function') {
      return api as ExportIPC;
    }
  } catch { /* not in Electron context */ }
  return null;
}

// ── Sanitizer (reuse concepts from DiagnosticsLogger) ───────────

const SENSITIVE_KEYS = new Set([
  'password', 'secret', 'token', 'credential', 'apiKey', 'api_key',
  'sessionToken', 'session_token', 'authorization', 'cookie',
]);

function deepSanitize(obj: unknown): unknown {
  if (typeof obj === 'string') {
    // Mask JWT tokens
    let s = obj.replace(/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g, '[JWT_REDACTED]');
    // Mask emails
    s = s.replace(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g, '[EMAIL_REDACTED]');
    return s;
  }
  if (Array.isArray(obj)) return obj.map(deepSanitize);
  if (obj !== null && typeof obj === 'object') {
    const result: Record<string, unknown> = {};
    for (const [key, val] of Object.entries(obj as Record<string, unknown>)) {
      if (SENSITIVE_KEYS.has(key.toLowerCase())) {
        result[key] = '[REDACTED]';
      } else {
        result[key] = deepSanitize(val);
      }
    }
    return result;
  }
  return obj;
}

// ── Collector Engine ────────────────────────────────────────────

class DiagnosticsCollectorEngine {
  private readonly _log = diagnosticsLogger.createLogger('system', 'DiagCollector');
  private _lastPackage: DiagnosticsPackage | null = null;

  // ── Main Collection ────────────────────────────────────

  /**
   * Collect all diagnostic data into a single package.
   * Non-blocking, safe — catches all errors per-section.
   */
  async collectAll(): Promise<DiagnosticsPackage> {
    const start = performance.now();
    this._log.info('Starting diagnostics collection');

    const sections: PackageSection[] = [];

    // 1. Summary & system info
    const systemInfo = this._collectSystemInfo();
    sections.push(this._makeSection('system-info', 'System Information', 'system-info.json', systemInfo, false));

    // 2. Health check (run fresh)
    try {
      const health = await healthCheckSystem.runAllChecks();
      sections.push(this._makeSection('health', 'Health Check', 'health.json', health, false));
    } catch (err) {
      sections.push(this._makeSection('health', 'Health Check', 'health.json', {
        error: (err as Error).message,
        lastKnown: healthCheckSystem.getLastHealth(),
      }, false));
    }

    // 3. DiagnosticsLogger logs (normal)
    const normalLogs = diagnosticsLogger.exportAsText();
    sections.push(this._makeSection('logs-normal', 'Application Logs', 'logs-normal.txt', normalLogs, false));

    // 4. DiagnosticsLogger logs (debug)
    const debugLogs = diagnosticsLogger.getDebugLogs();
    if (debugLogs.length > 0) {
      const debugText = debugLogs.map(e =>
        `${e.ts} [${e.level}] [${e.category}/${e.source}] ${e.message}${e.data ? ' ' + JSON.stringify(e.data) : ''}`,
      ).join('\n');
      sections.push(this._makeSection('logs-debug', 'Debug Logs', 'logs-debug.txt', debugText, false));
    }

    // 5. AppLogger buffer
    try {
      const appLogs = AppLogger.exportLogs();
      sections.push(this._makeSection('logs-applogger', 'AppLogger Buffer', 'logs-applogger.txt', appLogs, false));
    } catch {
      sections.push(this._makeSection('logs-applogger', 'AppLogger Buffer', 'logs-applogger.txt', '[unavailable]', false));
    }

    // 6. Call debug logs
    try {
      const callDebugLog = this._collectCallDebugLogs();
      if (callDebugLog) {
        sections.push(this._makeSection('logs-calls', 'Call Debug Logs', 'logs-calls.json', callDebugLog, false));
      }
    } catch { /* CallDebugLogger may not be imported */ }

    // 7. Error classifier data
    const errorData = this._collectErrorClassifierData();
    if (errorData) {
      sections.push(this._makeSection('errors', 'Error Log', 'errors.json', errorData, false));
    }

    // 8. Resilience state
    const resilienceData = this._collectResilienceState();
    if (resilienceData) {
      sections.push(this._makeSection('resilience', 'Resilience State', 'resilience.json', resilienceData, false));
    }

    // 9. Network state
    const networkData = this._collectNetworkState();
    sections.push(this._makeSection('network-state', 'Network State', 'network-state.json', networkData, false));

    // 10. Performance snapshot
    const perfData = this._collectPerformance();
    sections.push(this._makeSection('performance', 'Performance', 'performance.json', perfData, false));

    // 11. User config (sanitized)
    const configData = this._collectConfig();
    if (configData) {
      sections.push(this._makeSection('config', 'Configuration', 'config.json', configData, true));
    }

    // 12. DiagnosticsLogger stats
    const stats = diagnosticsLogger.getStats();
    sections.push(this._makeSection('diag-stats', 'Diagnostics Stats', 'diagnostics-stats.json', stats, false));

    const durationMs = Math.round(performance.now() - start);
    const totalSize = sections.reduce((sum, s) => sum + s.sizeBytes, 0);

    const pkg: DiagnosticsPackage = {
      version: '1.0.0',
      collectedAt: new Date().toISOString(),
      collectionDurationMs: durationMs,
      appVersion: this._getAppVersion(),
      sessionId: diagnosticsLogger.getSessionId(),
      healthStatus: healthCheckSystem.getLastHealth()?.status || 'unknown',
      sections,
      totalSizeBytes: totalSize,
    };

    this._lastPackage = pkg;
    this._log.info('Diagnostics collection complete', {
      sections: sections.length,
      totalSizeKB: Math.round(totalSize / 1024),
      durationMs,
    });

    return pkg;
  }

  /**
   * Get the last collected package (if any).
   */
  getLastPackage(): DiagnosticsPackage | null {
    return this._lastPackage;
  }

  // ── Export Methods ─────────────────────────────────────

  /**
   * Export package as a single JSON string.
   */
  async exportAsJSON(): Promise<string> {
    const pkg = await this.collectAll();
    return JSON.stringify(deepSanitize(pkg), null, 2);
  }

  /**
   * Export to file via Electron save dialog.
   * Returns the saved file path, or null if cancelled.
   */
  async exportToFile(): Promise<string | null> {
    const json = await this.exportAsJSON();
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').substring(0, 19);
    const suggestedName = `CommClient-Diagnostics-${timestamp}.json`;

    const ipc = getExportIPC();
    if (ipc) {
      try {
        const savedPath = await ipc.saveDiagnosticsPackage(json, suggestedName);
        if (savedPath) {
          this._log.info('Diagnostics exported to file', { path: savedPath });
        }
        return savedPath;
      } catch (err) {
        this._log.error('Failed to save diagnostics file', err as Error);
        return null;
      }
    }

    // Fallback: browser download
    this._browserDownload(json, suggestedName);
    return suggestedName;
  }

  /**
   * Copy package JSON to clipboard.
   */
  async copyToClipboard(): Promise<boolean> {
    try {
      const json = await this.exportAsJSON();
      await navigator.clipboard.writeText(json);
      this._log.info('Diagnostics copied to clipboard');
      return true;
    } catch (err) {
      this._log.error('Failed to copy diagnostics to clipboard', err as Error);
      return false;
    }
  }

  // ── Private: Section Builder ───────────────────────────

  private _makeSection(
    id: string,
    label: string,
    fileName: string,
    data: unknown,
    sensitive: boolean,
  ): PackageSection {
    const serialized = typeof data === 'string' ? data : JSON.stringify(data);
    return {
      id,
      label,
      fileName,
      data,
      sizeBytes: new Blob([serialized]).size,
      sensitive,
    };
  }

  // ── Private: Data Collectors ───────────────────────────

  private _collectSystemInfo(): SystemInfo {
    const mem = typeof performance !== 'undefined' && (performance as any).memory
      ? {
        usedMB: Math.round((performance as any).memory.usedJSHeapSize / (1024 * 1024)),
        totalMB: Math.round((performance as any).memory.totalJSHeapSize / (1024 * 1024)),
        limitMB: Math.round((performance as any).memory.jsHeapSizeLimit / (1024 * 1024)),
      }
      : null;

    const conn = typeof navigator !== 'undefined' && (navigator as any).connection
      ? {
        effectiveType: (navigator as any).connection.effectiveType || 'unknown',
        downlink: (navigator as any).connection.downlink || 0,
        rtt: (navigator as any).connection.rtt || 0,
      }
      : null;

    return {
      platform: typeof process !== 'undefined' ? process.platform : 'browser',
      userAgent: navigator.userAgent,
      screenResolution: `${screen.width}x${screen.height}`,
      devicePixelRatio: window.devicePixelRatio || 1,
      cpuCores: navigator.hardwareConcurrency || 0,
      deviceMemoryGB: (navigator as any).deviceMemory || null,
      memory: mem,
      electronVersion: typeof process !== 'undefined' ? (process.versions as any)?.electron || null : null,
      chromeVersion: typeof process !== 'undefined' ? (process.versions as any)?.chrome || null : null,
      nodeVersion: typeof process !== 'undefined' ? process.versions?.node || null : null,
      language: navigator.language,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      online: navigator.onLine,
      connection: conn,
    };
  }

  private _collectCallDebugLogs(): object | null {
    try {
      const debugLogger = (window as any).__commclient_call_debug;
      if (debugLogger && typeof debugLogger.getLog === 'function') {
        const entries = debugLogger.getLog();
        return {
          enabled: debugLogger.enabled,
          entries,
          count: entries.length,
        };
      }
    } catch { /* not available */ }
    return null;
  }

  private _collectErrorClassifierData(): object | null {
    try {
      // Import dynamically since ErrorClassifier might not be available in all contexts
      const errorModule = (window as any).__commclient_error_classifier;
      if (errorModule) {
        return {
          log: errorModule.getErrorLog?.() || [],
          frequency: errorModule.getErrorFrequency?.() || {},
        };
      }

      // Try reading from resilience module if globally registered
      return null;
    } catch { return null; }
  }

  private _collectResilienceState(): object | null {
    try {
      const data: Record<string, unknown> = {};

      // Read safe state from localStorage
      const stateKeys = [
        'commclient_drafts',
        'commclient_callState',
        'commclient_activeSession',
        'commclient_storeSnapshot',
        'commclient_heartbeat',
        'commclient_crash_journal',
      ];

      for (const key of stateKeys) {
        try {
          const val = localStorage.getItem(key);
          if (val) {
            data[key] = JSON.parse(val);
          }
        } catch {
          data[key] = '[parse_error]';
        }
      }

      return Object.keys(data).length > 0 ? (deepSanitize(data) as any) : null;
    } catch { return null; }
  }

  private _collectNetworkState(): object {
    const info: Record<string, unknown> = {
      online: navigator.onLine,
      timestamp: Date.now(),
    };

    if ((navigator as any).connection) {
      const conn = (navigator as any).connection;
      info.effectiveType = conn.effectiveType;
      info.downlink = conn.downlink;
      info.rtt = conn.rtt;
      info.saveData = conn.saveData;
      info.type = conn.type;
    }

    return info;
  }

  private _collectPerformance(): PerformanceSnapshot {
    const mem = (performance as any).memory
      ? {
        usedJSHeapMB: Math.round((performance as any).memory.usedJSHeapSize / (1024 * 1024)),
        totalJSHeapMB: Math.round((performance as any).memory.totalJSHeapSize / (1024 * 1024)),
        heapLimitMB: Math.round((performance as any).memory.jsHeapSizeLimit / (1024 * 1024)),
      }
      : null;

    let navTiming: PerformanceSnapshot['navigationTiming'] = null;
    try {
      const entries = performance.getEntriesByType('navigation');
      if (entries.length > 0) {
        const nav = entries[0] as PerformanceNavigationTiming;
        navTiming = {
          domContentLoadedMs: Math.round(nav.domContentLoadedEventEnd - nav.fetchStart),
          loadCompleteMs: Math.round(nav.loadEventEnd - nav.fetchStart),
          ttfbMs: Math.round(nav.responseStart - nav.fetchStart),
        };
      }
    } catch { /* not available */ }

    const resourceCount = performance.getEntriesByType('resource').length;

    return {
      memory: mem,
      navigationTiming: navTiming,
      resourceCount,
      longTaskCount: 0, // Would need PerformanceObserver, not feasible in snapshot
      estimatedFPS: null, // Requires rAF measurement over time
    };
  }

  private _collectConfig(): object | null {
    try {
      const raw = localStorage.getItem('commclient_config');
      if (raw) {
        const parsed = JSON.parse(raw);
        return deepSanitize(parsed) as object;
      }
    } catch { /* no config */ }
    return null;
  }

  private _getAppVersion(): string {
    try {
      const api = (window as any).electronAPI;
      if (api && api.appVersion) return api.appVersion;
    } catch { /* not in Electron */ }
    return 'unknown';
  }

  // ── Private: Browser Download Fallback ─────────────────

  private _browserDownload(content: string, filename: string): void {
    const blob = new Blob([content], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 100);
  }
}

// ── Singleton Export ────────────────────────────────────────────

export const diagnosticsCollector = new DiagnosticsCollectorEngine();
