/**
 * DebugLogger — structured debug logging for the call subsystem.
 *
 * Captures call lifecycle events with timestamps, WebRTC stats, and
 * state transitions for debugging LAN connectivity issues.
 *
 * Enable: localStorage.setItem('commclient_debug_calls', 'true')
 */

export interface CallLogEntry {
  timestamp: number;
  module: string;
  event: string;
  data?: Record<string, any>;
}

const MAX_LOG_ENTRIES = 500;
let _enabled = false;
const _log: CallLogEntry[] = [];

try {
  _enabled = localStorage.getItem('commclient_debug_calls') === 'true';
} catch {
  // SSR or no localStorage
}

export const CallDebugLogger = {
  get enabled(): boolean {
    return _enabled;
  },

  enable(): void {
    _enabled = true;
    try { localStorage.setItem('commclient_debug_calls', 'true'); } catch {}
  },

  disable(): void {
    _enabled = false;
    try { localStorage.removeItem('commclient_debug_calls'); } catch {}
  },

  log(module: string, event: string, data?: Record<string, any>): void {
    if (!_enabled) return;

    const entry: CallLogEntry = {
      timestamp: Date.now(),
      module,
      event,
      data,
    };

    _log.push(entry);
    if (_log.length > MAX_LOG_ENTRIES) _log.shift();

    console.debug(
      `[${module}] ${event}`,
      data ? JSON.stringify(data, null, 0) : ''
    );
  },

  /** Get all log entries (newest last) */
  getLog(): CallLogEntry[] {
    return [..._log];
  },

  /** Export as JSON for bug reports */
  exportJSON(): string {
    return JSON.stringify({
      exported_at: new Date().toISOString(),
      entries: _log,
      user_agent: navigator.userAgent,
    }, null, 2);
  },

  /** Clear the log buffer */
  clear(): void {
    _log.length = 0;
  },

  // ── Convenience methods ────────────────────────────

  fsm(from: string, to: string, event: string): void {
    this.log('FSM', 'transition', { from, to, event });
  },

  signal(direction: 'send' | 'recv', type: string, peerId: string): void {
    this.log('Signal', `${direction}:${type}`, { peerId });
  },

  ice(peerId: string, state: string): void {
    this.log('ICE', 'state_change', { peerId, state });
  },

  media(action: string, kind: string, trackId?: string): void {
    this.log('Media', action, { kind, trackId });
  },

  quality(level: string, score: number, metrics?: Record<string, any>): void {
    this.log('Quality', 'assessment', { level, score, ...metrics });
  },

  error(module: string, error: string, details?: Record<string, any>): void {
    this.log(module, 'ERROR', { error, ...details });
    console.error(`[${module}] ERROR: ${error}`, details);
  },
};

// Expose on window for debug console access
if (typeof window !== 'undefined') {
  (window as any).__commclient_call_debug = CallDebugLogger;
}
