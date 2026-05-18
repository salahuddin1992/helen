/**
 * ErrorClassifier.ts — Error taxonomy, severity model, and recovery routing.
 *
 * Classifies EVERY error that can occur in CommClient into a structured
 * taxonomy with severity levels and prescribed recovery actions.
 *
 * Error classification model:
 * ┌────────────────────────────────────────────────────────────────────────┐
 * │                        Error Severity Levels                           │
 * ├─────────┬──────────┬──────────────────────────────────────────────────┤
 * │ Level   │ Name     │ Behavior                                         │
 * ├─────────┼──────────┼──────────────────────────────────────────────────┤
 * │ 0       │ INFO     │ Log only. No action needed.                      │
 * │ 1       │ WARNING  │ Log + optional toast. Auto-recoverable.          │
 * │ 2       │ ERROR    │ Log + toast. User may need to retry an action.   │
 * │ 3       │ CRITICAL │ Log + modal. Feature degraded, core still works. │
 * │ 4       │ FATAL    │ Log + block UI. App must restart or reset.       │
 * └─────────┴──────────┴──────────────────────────────────────────────────┘
 *
 * Error domains:
 * ┌───────────────┬──────────────────────────────────────────────────────┐
 * │ Domain        │ Examples                                             │
 * ├───────────────┼──────────────────────────────────────────────────────┤
 * │ NETWORK       │ Socket disconnect, timeout, DNS, server unreachable  │
 * │ MEDIA         │ Camera denied, mic not found, codec failure          │
 * │ CALL          │ ICE failure, peer disconnect, SRTP error             │
 * │ DATABASE      │ SQLite lock, corruption, migration failure           │
 * │ STORAGE       │ localStorage full, file write failed, disk full      │
 * │ AUTH          │ Token expired, session invalid, server 401           │
 * │ UI            │ React render error, missing translation              │
 * │ SYSTEM        │ Out of memory, GPU crash, file system error          │
 * │ SERVER        │ Backend 500, backend crash, health check fail        │
 * │ TRANSFER      │ Upload failed, download timeout, chunk error         │
 * └───────────────┴──────────────────────────────────────────────────────┘
 *
 * Recovery routing:
 *   Each classified error maps to a prescribed recovery action from
 *   CrashRecoveryManager / NetworkResilienceEngine / SessionRecoveryEngine.
 */

// ── Types ───────────────────────────────────────────────────

export type ErrorSeverity = 0 | 1 | 2 | 3 | 4;
export type ErrorSeverityName = 'info' | 'warning' | 'error' | 'critical' | 'fatal';

export type ErrorDomain =
  | 'network'
  | 'media'
  | 'call'
  | 'database'
  | 'storage'
  | 'auth'
  | 'ui'
  | 'system'
  | 'server'
  | 'transfer';

export type RecoveryStrategy =
  | 'auto_retry'        // Automatic retry with backoff
  | 'reconnect_socket'  // Socket-level reconnection
  | 'reconnect_call'    // WebRTC reconnection (ICE restart)
  | 'restart_server'    // Restart backend server
  | 'user_retry'        // Show retry button to user
  | 'user_action'       // User must take specific action (grant permission, etc.)
  | 'degrade_feature'   // Disable the broken feature, keep app running
  | 'restart_app'       // Restart the Electron app
  | 'clean_slate'       // Reset all state and restart
  | 'log_only'          // No action — just log
  | 'ignore';           // Expected/harmless — don't even log

export interface ClassifiedError {
  /** Unique error code (domain.specific_error) */
  code: string;
  /** Error domain */
  domain: ErrorDomain;
  /** Severity level (0-4) */
  severity: ErrorSeverity;
  /** Severity name */
  severityName: ErrorSeverityName;
  /** Human-readable description */
  description: string;
  /** i18n key for user-facing message */
  messageKey: string;
  /** Prescribed recovery strategy */
  recovery: RecoveryStrategy;
  /** Whether to show the user a notification */
  showToast: boolean;
  /** Whether to show a blocking modal */
  showModal: boolean;
  /** Whether this error is retryable */
  retryable: boolean;
  /** Max automatic retries (0 = no auto-retry) */
  maxAutoRetries: number;
  /** Original error for debugging */
  originalError: Error | null;
  /** Timestamp */
  timestamp: number;
  /** Additional context */
  context: Record<string, unknown>;
}

// ── Severity Helpers ────────────────────────────────────────

const SEVERITY_NAMES: Record<ErrorSeverity, ErrorSeverityName> = {
  0: 'info',
  1: 'warning',
  2: 'error',
  3: 'critical',
  4: 'fatal',
};

// ── Error Code Registry ─────────────────────────────────────

interface ErrorDefinition {
  domain: ErrorDomain;
  severity: ErrorSeverity;
  description: string;
  messageKey: string;
  recovery: RecoveryStrategy;
  showToast: boolean;
  showModal: boolean;
  retryable: boolean;
  maxAutoRetries: number;
}

/**
 * Complete error code registry. Every known error has a definition.
 */
const ERROR_REGISTRY: Record<string, ErrorDefinition> = {
  // ── NETWORK ─────────────────────────────────────────────
  'network.socket_disconnect': {
    domain: 'network', severity: 1,
    description: 'Socket.IO connection lost',
    messageKey: 'resilience.socket_disconnect',
    recovery: 'reconnect_socket', showToast: false, showModal: false,
    retryable: true, maxAutoRetries: 50,
  },
  'network.socket_timeout': {
    domain: 'network', severity: 2,
    description: 'Socket.IO connection timeout',
    messageKey: 'resilience.socket_timeout',
    recovery: 'reconnect_socket', showToast: true, showModal: false,
    retryable: true, maxAutoRetries: 10,
  },
  'network.server_unreachable': {
    domain: 'network', severity: 3,
    description: 'Cannot reach the backend server',
    messageKey: 'resilience.server_unreachable',
    recovery: 'reconnect_socket', showToast: true, showModal: false,
    retryable: true, maxAutoRetries: 20,
  },
  'network.navigator_offline': {
    domain: 'network', severity: 2,
    description: 'Device is offline (no network)',
    messageKey: 'resilience.device_offline',
    recovery: 'auto_retry', showToast: true, showModal: false,
    retryable: true, maxAutoRetries: 0, // Wait for online event
  },
  'network.lan_not_found': {
    domain: 'network', severity: 3,
    description: 'No LAN/WiFi network detected',
    messageKey: 'resilience.no_lan',
    recovery: 'user_action', showToast: true, showModal: true,
    retryable: false, maxAutoRetries: 0,
  },

  // ── MEDIA ───────────────────────────────────────────────
  'media.camera_denied': {
    domain: 'media', severity: 2,
    description: 'Camera access denied by user or system',
    messageKey: 'resilience.camera_denied',
    recovery: 'user_action', showToast: true, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },
  'media.mic_denied': {
    domain: 'media', severity: 2,
    description: 'Microphone access denied',
    messageKey: 'resilience.mic_denied',
    recovery: 'user_action', showToast: true, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },
  'media.device_not_found': {
    domain: 'media', severity: 2,
    description: 'Requested media device not found',
    messageKey: 'resilience.device_not_found',
    recovery: 'user_action', showToast: true, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },
  'media.codec_unsupported': {
    domain: 'media', severity: 1,
    description: 'Preferred codec not supported — using fallback',
    messageKey: 'resilience.codec_fallback',
    recovery: 'degrade_feature', showToast: false, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },
  'media.screenshare_denied': {
    domain: 'media', severity: 1,
    description: 'Screen share cancelled or denied',
    messageKey: 'resilience.screenshare_denied',
    recovery: 'log_only', showToast: false, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },

  // ── CALL ────────────────────────────────────────────────
  'call.ice_failed': {
    domain: 'call', severity: 2,
    description: 'ICE connection failed — cannot establish peer connection',
    messageKey: 'resilience.ice_failed',
    recovery: 'reconnect_call', showToast: true, showModal: false,
    retryable: true, maxAutoRetries: 5,
  },
  'call.peer_disconnect': {
    domain: 'call', severity: 1,
    description: 'Peer connection interrupted',
    messageKey: 'resilience.peer_disconnect',
    recovery: 'reconnect_call', showToast: false, showModal: false,
    retryable: true, maxAutoRetries: 5,
  },
  'call.peer_left': {
    domain: 'call', severity: 0,
    description: 'Remote peer left the call',
    messageKey: 'resilience.peer_left',
    recovery: 'log_only', showToast: true, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },
  'call.timeout': {
    domain: 'call', severity: 2,
    description: 'Call setup or reconnect timeout',
    messageKey: 'resilience.call_timeout',
    recovery: 'user_retry', showToast: true, showModal: false,
    retryable: true, maxAutoRetries: 2,
  },
  'call.quality_degraded': {
    domain: 'call', severity: 1,
    description: 'Call quality reduced due to network conditions',
    messageKey: 'resilience.call_quality_low',
    recovery: 'degrade_feature', showToast: true, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },

  // ── DATABASE ────────────────────────────────────────────
  'database.locked': {
    domain: 'database', severity: 1,
    description: 'SQLite database locked — another process may be using it',
    messageKey: 'resilience.db_locked',
    recovery: 'auto_retry', showToast: false, showModal: false,
    retryable: true, maxAutoRetries: 10,
  },
  'database.corrupt': {
    domain: 'database', severity: 4,
    description: 'SQLite database corruption detected',
    messageKey: 'resilience.db_corrupt',
    recovery: 'restart_app', showToast: false, showModal: true,
    retryable: false, maxAutoRetries: 0,
  },
  'database.migration_failed': {
    domain: 'database', severity: 3,
    description: 'Database schema migration failed',
    messageKey: 'resilience.db_migration_failed',
    recovery: 'restart_app', showToast: false, showModal: true,
    retryable: false, maxAutoRetries: 0,
  },

  // ── STORAGE ─────────────────────────────────────────────
  'storage.localstorage_full': {
    domain: 'storage', severity: 2,
    description: 'localStorage quota exceeded',
    messageKey: 'resilience.storage_full',
    recovery: 'degrade_feature', showToast: true, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },
  'storage.file_write_failed': {
    domain: 'storage', severity: 2,
    description: 'Failed to write file to disk',
    messageKey: 'resilience.file_write_failed',
    recovery: 'user_retry', showToast: true, showModal: false,
    retryable: true, maxAutoRetries: 3,
  },
  'storage.disk_full': {
    domain: 'storage', severity: 3,
    description: 'Disk space critically low',
    messageKey: 'resilience.disk_full',
    recovery: 'user_action', showToast: true, showModal: true,
    retryable: false, maxAutoRetries: 0,
  },

  // ── AUTH ────────────────────────────────────────────────
  'auth.token_expired': {
    domain: 'auth', severity: 2,
    description: 'Authentication token expired',
    messageKey: 'resilience.token_expired',
    recovery: 'user_action', showToast: true, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },
  'auth.session_invalid': {
    domain: 'auth', severity: 2,
    description: 'Session is no longer valid',
    messageKey: 'resilience.session_invalid',
    recovery: 'user_action', showToast: true, showModal: true,
    retryable: false, maxAutoRetries: 0,
  },

  // ── UI ──────────────────────────────────────────────────
  'ui.render_error': {
    domain: 'ui', severity: 2,
    description: 'React component render error',
    messageKey: 'resilience.render_error',
    recovery: 'degrade_feature', showToast: false, showModal: false,
    retryable: true, maxAutoRetries: 1,
  },
  'ui.missing_translation': {
    domain: 'ui', severity: 0,
    description: 'Missing translation key',
    messageKey: '', // Not shown to user
    recovery: 'ignore', showToast: false, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },

  // ── SYSTEM ──────────────────────────────────────────────
  'system.out_of_memory': {
    domain: 'system', severity: 4,
    description: 'Application ran out of memory',
    messageKey: 'resilience.out_of_memory',
    recovery: 'restart_app', showToast: false, showModal: true,
    retryable: false, maxAutoRetries: 0,
  },
  'system.gpu_crash': {
    domain: 'system', severity: 3,
    description: 'GPU process crashed',
    messageKey: 'resilience.gpu_crash',
    recovery: 'degrade_feature', showToast: true, showModal: false,
    retryable: false, maxAutoRetries: 0,
  },

  // ── SERVER ──────────────────────────────────────────────
  'server.crash': {
    domain: 'server', severity: 3,
    description: 'Backend server crashed',
    messageKey: 'resilience.server_crash',
    recovery: 'restart_server', showToast: true, showModal: true,
    retryable: true, maxAutoRetries: 3,
  },
  'server.health_timeout': {
    domain: 'server', severity: 2,
    description: 'Server health check timed out',
    messageKey: 'resilience.server_slow',
    recovery: 'auto_retry', showToast: false, showModal: false,
    retryable: true, maxAutoRetries: 5,
  },
  'server.internal_error': {
    domain: 'server', severity: 2,
    description: 'Server returned 500 Internal Server Error',
    messageKey: 'resilience.server_error',
    recovery: 'auto_retry', showToast: true, showModal: false,
    retryable: true, maxAutoRetries: 3,
  },

  // ── TRANSFER ────────────────────────────────────────────
  'transfer.upload_failed': {
    domain: 'transfer', severity: 2,
    description: 'File upload failed',
    messageKey: 'resilience.upload_failed',
    recovery: 'user_retry', showToast: true, showModal: false,
    retryable: true, maxAutoRetries: 3,
  },
  'transfer.download_timeout': {
    domain: 'transfer', severity: 2,
    description: 'File download timed out',
    messageKey: 'resilience.download_timeout',
    recovery: 'user_retry', showToast: true, showModal: false,
    retryable: true, maxAutoRetries: 3,
  },
  'transfer.chunk_error': {
    domain: 'transfer', severity: 1,
    description: 'File transfer chunk error — retrying',
    messageKey: 'resilience.chunk_error',
    recovery: 'auto_retry', showToast: false, showModal: false,
    retryable: true, maxAutoRetries: 5,
  },
};

// ── Classification Function ─────────────────────────────────

/**
 * Classify an error by its code. Returns full ClassifiedError.
 */
export function classifyByCode(
  code: string,
  originalError?: Error,
  context?: Record<string, unknown>,
): ClassifiedError {
  const def = ERROR_REGISTRY[code];
  if (!def) {
    // Unknown error — default to severity 2
    return {
      code,
      domain: 'system',
      severity: 2,
      severityName: 'error',
      description: `Unknown error: ${code}`,
      messageKey: 'resilience.unknown_error',
      recovery: 'log_only',
      showToast: true,
      showModal: false,
      retryable: false,
      maxAutoRetries: 0,
      originalError: originalError ?? null,
      timestamp: Date.now(),
      context: context ?? {},
    };
  }

  return {
    code,
    domain: def.domain,
    severity: def.severity,
    severityName: SEVERITY_NAMES[def.severity],
    description: def.description,
    messageKey: def.messageKey,
    recovery: def.recovery,
    showToast: def.showToast,
    showModal: def.showModal,
    retryable: def.retryable,
    maxAutoRetries: def.maxAutoRetries,
    originalError: originalError ?? null,
    timestamp: Date.now(),
    context: context ?? {},
  };
}

/**
 * Auto-classify an error from its message/type. Best-effort heuristic.
 */
export function classifyFromError(error: Error, context?: Record<string, unknown>): ClassifiedError {
  const msg = error.message.toLowerCase();
  const name = error.name.toLowerCase();

  // Network errors
  if (msg.includes('networkerror') || msg.includes('failed to fetch') || msg.includes('econnrefused')) {
    return classifyByCode('network.server_unreachable', error, context);
  }
  if (msg.includes('timeout') && msg.includes('socket')) {
    return classifyByCode('network.socket_timeout', error, context);
  }
  if (msg.includes('offline')) {
    return classifyByCode('network.navigator_offline', error, context);
  }

  // Media errors
  if (name === 'notallowederror' && msg.includes('camera')) {
    return classifyByCode('media.camera_denied', error, context);
  }
  if (name === 'notallowederror' && (msg.includes('microphone') || msg.includes('audio'))) {
    return classifyByCode('media.mic_denied', error, context);
  }
  if (name === 'notfounderror' && msg.includes('device')) {
    return classifyByCode('media.device_not_found', error, context);
  }

  // Storage errors
  if (name === 'quotaexceedederror' || msg.includes('quota')) {
    return classifyByCode('storage.localstorage_full', error, context);
  }

  // Database errors
  if (msg.includes('database') && msg.includes('locked')) {
    return classifyByCode('database.locked', error, context);
  }
  if (msg.includes('database') && (msg.includes('corrupt') || msg.includes('malformed'))) {
    return classifyByCode('database.corrupt', error, context);
  }

  // ICE/WebRTC errors
  if (msg.includes('ice') && msg.includes('failed')) {
    return classifyByCode('call.ice_failed', error, context);
  }

  // Auth errors
  if (msg.includes('401') || msg.includes('unauthorized') || msg.includes('token')) {
    return classifyByCode('auth.token_expired', error, context);
  }

  // Memory errors
  if (msg.includes('out of memory') || name === 'rangeerror') {
    return classifyByCode('system.out_of_memory', error, context);
  }

  // Unknown — default classification
  return classifyByCode('_unknown', error, context);
}

/**
 * Check if an error severity requires user attention.
 */
export function requiresUserAttention(severity: ErrorSeverity): boolean {
  return severity >= 2;
}

/**
 * Check if an error is fatal (app should restart).
 */
export function isFatal(severity: ErrorSeverity): boolean {
  return severity >= 4;
}

/**
 * Get all error codes for a specific domain.
 */
export function getErrorsByDomain(domain: ErrorDomain): string[] {
  return Object.entries(ERROR_REGISTRY)
    .filter(([, def]) => def.domain === domain)
    .map(([code]) => code);
}

/**
 * Get all error codes at or above a severity level.
 */
export function getErrorsBySeverity(minSeverity: ErrorSeverity): string[] {
  return Object.entries(ERROR_REGISTRY)
    .filter(([, def]) => def.severity >= minSeverity)
    .map(([code]) => code);
}

// ── Error Log Ring Buffer ───────────────────────────────────

const MAX_LOG_ENTRIES = 100;
const errorLog: ClassifiedError[] = [];

/**
 * Log a classified error to the ring buffer.
 */
export function logError(classified: ClassifiedError): void {
  errorLog.push(classified);
  if (errorLog.length > MAX_LOG_ENTRIES) {
    errorLog.shift();
  }

  // Console output based on severity
  const prefix = `[${classified.severityName.toUpperCase()}] [${classified.domain}]`;
  switch (classified.severity) {
    case 0: console.debug(prefix, classified.description); break;
    case 1: console.warn(prefix, classified.description); break;
    case 2: console.error(prefix, classified.description); break;
    case 3: console.error(prefix, '⚠️', classified.description); break;
    case 4: console.error(prefix, '🔴 FATAL:', classified.description); break;
  }
}

/**
 * Get recent error log entries.
 */
export function getErrorLog(limit = 50): ClassifiedError[] {
  return errorLog.slice(-limit);
}

/**
 * Get error frequency by domain in the last N minutes.
 */
export function getErrorFrequency(windowMinutes = 5): Record<ErrorDomain, number> {
  const cutoff = Date.now() - windowMinutes * 60 * 1000;
  const freq: Record<string, number> = {};

  for (const entry of errorLog) {
    if (entry.timestamp >= cutoff) {
      freq[entry.domain] = (freq[entry.domain] || 0) + 1;
    }
  }

  return freq as Record<ErrorDomain, number>;
}
