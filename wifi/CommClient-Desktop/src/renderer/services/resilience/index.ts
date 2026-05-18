/**
 * resilience/ — Phase 13: Desktop Resilience & Recovery Engineering
 *
 * ┌──────────────────────────────────────────────────────────────────────────┐
 * │                  CommClient Resilience Architecture                       │
 * │                                                                          │
 * │  ┌─────────────────────────────────────────────────────────────────────┐ │
 * │  │                         App Startup                                 │ │
 * │  │  1. CrashRecoveryManager.initialize()                              │ │
 * │  │     → detect crash tier (clean / soft / hard / corrupt / fatal)     │ │
 * │  │  2. SafeStateManager.startAutoSave()                               │ │
 * │  │     → resume debounced writes for all state categories             │ │
 * │  │  3. SessionRecoveryEngine.recover(crashRecord)                     │ │
 * │  │     → restore drafts, pending msgs, UI state, call rejoin prompt   │ │
 * │  │  4. NetworkResilienceEngine.start(callbacks)                       │ │
 * │  │     → begin monitoring, RTT probes, interface watch                │ │
 * │  └─────────────────────────────────────────────────────────────────────┘ │
 * │                                                                          │
 * │  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────────────┐ │
 * │  │ CrashRecovery    │  │ SafeState        │  │ ErrorClassifier       │ │
 * │  │ Manager          │  │ Manager          │  │                       │ │
 * │  │                  │  │                  │  │ • 10 error domains    │ │
 * │  │ • Heartbeat      │  │ • Debounced LS   │  │ • 5 severity levels  │ │
 * │  │ • Crash journal  │  │ • Checksum       │  │ • 35+ error codes    │ │
 * │  │ • Tier detection │  │ • Auto-persist   │  │ • Recovery routing   │ │
 * │  │ • Recovery plan  │  │ • Eviction       │  │ • Ring buffer log    │ │
 * │  │ • Plan executor  │  │ • Diagnostics    │  │ • Frequency analysis │ │
 * │  └────────┬─────────┘  └────────┬─────────┘  └───────────┬───────────┘ │
 * │           │                      │                         │             │
 * │           ▼                      ▼                         ▼             │
 * │  ┌──────────────────┐  ┌──────────────────┐                             │
 * │  │ SessionRecovery  │  │ NetworkResilience│                             │
 * │  │ Engine           │  │ Engine           │                             │
 * │  │                  │  │                  │                             │
 * │  │ • Call rejoin    │  │ • State machine  │                             │
 * │  │ • Draft restore  │  │ • Backoff retry  │                             │
 * │  │ • Pending resend │  │ • Msg queue      │                             │
 * │  │ • Transfer resume│  │ • RTT probe      │                             │
 * │  │ • UI state       │  │ • WiFi sleep     │                             │
 * │  │ • Time-bounded   │  │ • Interface Δ    │                             │
 * │  └──────────────────┘  └──────────────────┘                             │
 * │                                                                          │
 * │  Data flow on crash:                                                     │
 * │    crash → heartbeat stuck "running" → next launch detects               │
 * │    → reads journal → classifies tier → builds plan → executes            │
 * │    → restores drafts → reconnects socket → offers call rejoin            │
 * │                                                                          │
 * │  Data flow on network loss:                                              │
 * │    socket disconnect → NetworkResilienceEngine.onSocketDisconnect()       │
 * │    → queues outbound messages → exponential backoff retry                │
 * │    → navigator.online → instant retry → flush queue → restore call       │
 * └──────────────────────────────────────────────────────────────────────────┘
 */

// ── CrashRecoveryManager ────────────────────────────────────
export {
  crashRecoveryManager,
  type CrashTier,
  type ShutdownReason,
  type CrashRecord,
  type RecoveryPlan,
  type RecoveryAction,
  type RecoveryResult,
} from './CrashRecoveryManager';

// ── SafeStateManager ────────────────────────────────────────
export {
  safeStateManager,
  STATE_CONFIGS,
  type StateSnapshot,
  type PersistenceConfig,
  type WriteResult,
} from './SafeStateManager';

// ── SessionRecoveryEngine ───────────────────────────────────
export {
  sessionRecoveryEngine,
  type CallRecoveryInfo,
  type DraftRecoveryInfo,
  type PendingMessage,
  type TransferRecoveryInfo,
  type UIStateRecovery,
  type SessionRecoveryResult,
} from './SessionRecoveryEngine';

// ── NetworkResilienceEngine ─────────────────────────────────
export {
  networkResilienceEngine,
  type NetworkState,
  type NetworkEvent,
  type RetryConfig,
  type DisconnectContext,
  type ReconnectAttempt,
  type PendingOutboundMessage,
  type NetworkMetrics,
} from './NetworkResilienceEngine';

// ── ErrorClassifier ─────────────────────────────────────────
export {
  classifyByCode,
  classifyFromError,
  requiresUserAttention,
  isFatal,
  getErrorsByDomain,
  getErrorsBySeverity,
  logError,
  getErrorLog,
  getErrorFrequency,
  type ErrorSeverity,
  type ErrorSeverityName,
  type ErrorDomain,
  type RecoveryStrategy,
  type ClassifiedError,
} from './ErrorClassifier';
