/**
 * AppLogger — unified structured logging for the CommClient desktop app.
 *
 * Features:
 *   - Namespaced loggers (one per module)
 *   - Log level filtering (DEBUG/INFO/WARN/ERROR)
 *   - Structured output with timestamps
 *   - Optional file logging via Electron IPC (production)
 *   - Console grouping for related events
 *   - Performance timing helpers
 *   - Circular buffer for recent logs (accessible for bug reports)
 *
 * Usage:
 *   const log = AppLogger.create('CallEngine');
 *   log.info('Call started', { callId: '123', type: 'video' });
 *   log.error('ICE failed', error);
 *   const timer = log.time('sdp-negotiation');
 *   // ... do work ...
 *   timer.end();   // logs: "[CallEngine] sdp-negotiation completed in 142ms"
 */

export type LogLevel = 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';

const LOG_LEVEL_PRIORITY: Record<LogLevel, number> = {
  DEBUG: 0,
  INFO: 1,
  WARN: 2,
  ERROR: 3,
};

// ── Configuration ──────────────────────────────────

interface LogConfig {
  /** Minimum log level to output */
  minLevel: LogLevel;
  /** Enable console output */
  consoleEnabled: boolean;
  /** Enable buffer storage for recent logs */
  bufferEnabled: boolean;
  /** Max entries in the circular buffer */
  bufferSize: number;
  /** Enable timestamps in output */
  timestamps: boolean;
  /** Color-code by namespace in console */
  colorEnabled: boolean;
}

const defaultConfig: LogConfig = {
  minLevel: 'DEBUG',
  consoleEnabled: true,
  bufferEnabled: true,
  bufferSize: 500,
  timestamps: true,
  colorEnabled: true,
};

let _config: LogConfig = { ...defaultConfig };

// ── Circular Log Buffer ────────────────────────────

interface LogEntry {
  timestamp: string;
  level: LogLevel;
  namespace: string;
  message: string;
  data?: any;
}

const _buffer: LogEntry[] = [];
let _bufferIndex = 0;

function _addToBuffer(entry: LogEntry): void {
  if (!_config.bufferEnabled) return;

  if (_buffer.length < _config.bufferSize) {
    _buffer.push(entry);
  } else {
    _buffer[_bufferIndex] = entry;
  }
  _bufferIndex = (_bufferIndex + 1) % _config.bufferSize;
}

// ── Namespace Color Map ────────────────────────────

const NAMESPACE_COLORS = [
  '#3B82F6', // blue
  '#10B981', // emerald
  '#F59E0B', // amber
  '#8B5CF6', // violet
  '#EC4899', // pink
  '#06B6D4', // cyan
  '#F97316', // orange
  '#14B8A6', // teal
  '#6366F1', // indigo
  '#84CC16', // lime
];

const _colorMap = new Map<string, string>();

function _getColor(namespace: string): string {
  if (!_colorMap.has(namespace)) {
    const idx = _colorMap.size % NAMESPACE_COLORS.length;
    _colorMap.set(namespace, NAMESPACE_COLORS[idx]);
  }
  return _colorMap.get(namespace)!;
}

// ── Logger Instance ────────────────────────────────

interface TimerHandle {
  end: () => void;
}

export interface Logger {
  debug: (...args: any[]) => void;
  info: (...args: any[]) => void;
  warn: (...args: any[]) => void;
  error: (...args: any[]) => void;
  time: (label: string) => TimerHandle;
  group: (label: string) => void;
  groupEnd: () => void;
}

function _shouldLog(level: LogLevel): boolean {
  return LOG_LEVEL_PRIORITY[level] >= LOG_LEVEL_PRIORITY[_config.minLevel];
}

function _formatTimestamp(): string {
  const now = new Date();
  return now.toISOString().substring(11, 23); // HH:mm:ss.SSS
}

function _createLogger(namespace: string): Logger {
  const color = _getColor(namespace);

  function _log(level: LogLevel, args: any[]): void {
    if (!_shouldLog(level)) return;

    const ts = _config.timestamps ? _formatTimestamp() : '';
    const prefix = `[${namespace}]`;

    // Build message string for buffer
    const msgParts = args.map((a) =>
      typeof a === 'object' ? JSON.stringify(a) : String(a),
    );
    const message = msgParts.join(' ');

    // Add to circular buffer
    _addToBuffer({
      timestamp: ts,
      level,
      namespace,
      message,
      data: args.length === 1 && typeof args[0] === 'object' ? args[0] : undefined,
    });

    // Console output
    if (!_config.consoleEnabled) return;

    const consoleFn =
      level === 'ERROR'
        ? console.error
        : level === 'WARN'
          ? console.warn
          : level === 'DEBUG'
            ? console.debug
            : console.log;

    if (_config.colorEnabled) {
      consoleFn(
        `%c${ts} %c${prefix}%c ${level}`,
        'color: #666',
        `color: ${color}; font-weight: bold`,
        `color: ${level === 'ERROR' ? '#EF4444' : level === 'WARN' ? '#F59E0B' : '#9CA3AF'}`,
        ...args,
      );
    } else {
      consoleFn(`${ts} ${prefix} [${level}]`, ...args);
    }
  }

  return {
    debug: (...args: any[]) => _log('DEBUG', args),
    info: (...args: any[]) => _log('INFO', args),
    warn: (...args: any[]) => _log('WARN', args),
    error: (...args: any[]) => _log('ERROR', args),

    time(label: string): TimerHandle {
      const start = performance.now();
      return {
        end: () => {
          const elapsed = (performance.now() - start).toFixed(1);
          _log('INFO', [`${label} completed in ${elapsed}ms`]);
        },
      };
    },

    group(label: string): void {
      if (_config.consoleEnabled) {
        console.group(`%c[${namespace}]%c ${label}`, `color: ${color}; font-weight: bold`, '');
      }
    },

    groupEnd(): void {
      if (_config.consoleEnabled) {
        console.groupEnd();
      }
    },
  };
}

// ── Public API ─────────────────────────────────────

export const AppLogger = {
  /**
   * Create a namespaced logger instance.
   */
  create(namespace: string): Logger {
    return _createLogger(namespace);
  },

  /**
   * Update logger configuration.
   */
  configure(config: Partial<LogConfig>): void {
    _config = { ..._config, ...config };
  },

  /**
   * Set minimum log level.
   */
  setLevel(level: LogLevel): void {
    _config.minLevel = level;
  },

  /**
   * Get recent log entries from the circular buffer.
   * Useful for generating bug reports.
   */
  getRecentLogs(count?: number): LogEntry[] {
    const total = Math.min(count || _config.bufferSize, _buffer.length);
    const result: LogEntry[] = [];

    // Read from circular buffer in chronological order
    const startIdx =
      _buffer.length < _config.bufferSize
        ? 0
        : _bufferIndex;

    for (let i = 0; i < total; i++) {
      const idx = (startIdx + _buffer.length - total + i) % _buffer.length;
      if (_buffer[idx]) result.push(_buffer[idx]);
    }

    return result;
  },

  /**
   * Export all recent logs as a formatted string (for bug reports).
   */
  exportLogs(): string {
    const entries = this.getRecentLogs();
    return entries
      .map((e) => `${e.timestamp} [${e.level}] [${e.namespace}] ${e.message}`)
      .join('\n');
  },

  /**
   * Clear the log buffer.
   */
  clearBuffer(): void {
    _buffer.length = 0;
    _bufferIndex = 0;
  },

  /**
   * Get count of logs by level.
   */
  getStats(): Record<LogLevel, number> {
    const stats: Record<LogLevel, number> = { DEBUG: 0, INFO: 0, WARN: 0, ERROR: 0 };
    for (const entry of _buffer) {
      if (entry) stats[entry.level]++;
    }
    return stats;
  },
};

export default AppLogger;
