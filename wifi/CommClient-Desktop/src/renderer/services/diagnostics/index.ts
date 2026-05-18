/**
 * diagnostics/ — Phase 14: Desktop Diagnostics & Observability Engineering
 *
 * ┌──────────────────────────────────────────────────────────────────────────┐
 * │                  CommClient Diagnostics Architecture                      │
 * │                                                                          │
 * │  ┌─────────────────────────────────────────────────────────────────────┐ │
 * │  │                         App Startup                                 │ │
 * │  │  1. diagnosticsLogger.start()                                      │ │
 * │  │     → session ID, ring buffers, batch flush timer                  │ │
 * │  │  2. healthCheckSystem.start()                                      │ │
 * │  │     → periodic 5-subsystem checks every 10s                        │ │
 * │  │  3. Register providers (socket, error rate, uptime)                │ │
 * │  │  4. Components use createLogger(category, source)                  │ │
 * │  └─────────────────────────────────────────────────────────────────────┘ │
 * │                                                                          │
 * │  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────────────┐ │
 * │  │ DiagnosticsLogger│  │ HealthCheckSystem│  │ DiagnosticsCollector  │ │
 * │  │                  │  │                  │  │                       │ │
 * │  │ • 12 categories  │  │ • 5 subsystems   │  │ • 12 sections        │ │
 * │  │ • 5 log levels   │  │ • 4 status tiers │  │ • Privacy sanitizer  │ │
 * │  │ • Dual ring bufs │  │ • 20+ checks     │  │ • File/clipboard     │ │
 * │  │ • Batched IPC    │  │ • Aggregator     │  │ • Browser fallback   │ │
 * │  │ • Privacy strip  │  │ • Change events  │  │ • Package builder    │ │
 * │  │ • Debug toggle   │  │ • Threshold cfg  │  │ • Version tagged     │ │
 * │  └────────┬─────────┘  └────────┬─────────┘  └───────────┬───────────┘ │
 * │           │                      │                         │             │
 * │           └──────────────────────┼─────────────────────────┘             │
 * │                                  ▼                                       │
 * │                    ┌──────────────────────────┐                          │
 * │                    │   DiagnosticsScreen.tsx   │                          │
 * │                    │                          │                          │
 * │                    │ • Health dashboard        │                          │
 * │                    │ • Live log stream         │                          │
 * │                    │ • Category filters        │                          │
 * │                    │ • Stats & counters        │                          │
 * │                    │ • Export controls          │                          │
 * │                    │ • Debug mode toggle       │                          │
 * │                    │ • Advanced Mode gated     │                          │
 * │                    └──────────────────────────┘                          │
 * │                                                                          │
 * │  Data flow:                                                              │
 * │    component ──log()──► DiagnosticsLogger ──batch──► IPC ──► File        │
 * │                            │                                             │
 * │                            ├──► Ring Buffer ──► DiagnosticsScreen        │
 * │                            │                                             │
 * │                            └──► DiagnosticsCollector ──► Export Package  │
 * │                                                                          │
 * │  Integration with existing systems:                                      │
 * │    • AppLogger (Phase 1) — buffer exported in diagnostics package        │
 * │    • CallDebugLogger (Phase 4) — entries included in export              │
 * │    • ErrorClassifier (Phase 13) — error log + frequency in export        │
 * │    • CrashRecoveryManager (Phase 13) — crash journal in export           │
 * │    • NetworkResilienceEngine (Phase 13) — network state in export        │
 * │    • SafeStateManager (Phase 13) — persisted state in export             │
 * │    • AdvancedDashboard (Phase 7) — DiagnosticsScreen as companion view   │
 * │    • app-mode.store (Phase 7) — Advanced Mode gate for UI access         │
 * └──────────────────────────────────────────────────────────────────────────┘
 */

// ── DiagnosticsLogger ───────────────────────────────────────────
export {
  diagnosticsLogger,
  LOG_CATEGORY_LABELS,
  type DiagLogLevel,
  type LogCategory,
  type DiagLogEntry,
  type DiagLogConfig,
  type DiagnosticsStats,
  type CategoryLogger,
} from './DiagnosticsLogger';

// ── HealthCheckSystem ───────────────────────────────────────────
export {
  healthCheckSystem,
  type HealthStatus,
  type SubsystemName,
  type SubsystemHealth,
  type HealthCheck,
  type OverallHealth,
  type HealthChangeCallback,
  type HealthCheckConfig,
} from './HealthCheckSystem';

// ── DiagnosticsCollector ────────────────────────────────────────
export {
  diagnosticsCollector,
  type PackageSection,
  type DiagnosticsPackage,
  type SystemInfo,
  type PerformanceSnapshot,
} from './DiagnosticsCollector';

// ── DiagnosticsScreen ───────────────────────────────────────────
export { DiagnosticsScreen } from './DiagnosticsScreen';
