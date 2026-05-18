/**
 * Performance optimization subsystem — barrel exports.
 *
 * Architecture (Phase 6 — existing):
 *
 *   DeviceCapabilityDetector  ─┐
 *   NetworkQualityMonitor     ─┤──▶ GracefulDegradationEngine ──▶ DegradationState
 *   PerformanceGuard (CPU)    ─┘         │
 *                                        ├──▶ GroupCallOptimizer
 *                                        ├──▶ ScreenShareScaler
 *                                        └──▶ UI hints (animations, batching)
 *
 * Architecture (Phase 9 — compatibility layer):
 *
 *   HardwareProfiles (config) ───┐
 *   DeviceCapabilityDetector ────┤
 *   PerformanceGuard (metrics) ──┤──▶ AutoPerformanceManager ──┐
 *                                │       (orchestrator)         │
 *                                │                              ├──▶ ResourceGovernor (budgets)
 *                                │                              ├──▶ RenderOptimizer (CSS/DOM)
 *                                │                              ├──▶ MediaBudgetController (audio-first)
 *                                │                              └──▶ BackgroundThrottler (idle/bg)
 *                                │
 *   User mode selection ─────────┘  (Eco / Balanced / Performance)
 */

// Device capability detection
export {
  detectDeviceCapabilities,
  getCachedProfile,
  resetDetection,
  getTierCeiling,
  type DeviceTier,
  type DeviceProfile,
} from './DeviceCapabilityDetector';

// Network quality monitoring
export {
  NetworkQualityMonitor,
  type NetworkQuality,
  type NetworkSnapshot,
  type NetworkEvent,
} from './NetworkQualityMonitor';

// Graceful degradation engine
export {
  GracefulDegradationEngine,
  type DegradationLevel,
  type DegradationState,
  type DegradationAction,
  type DegradationReason,
  type MediaConstraints,
  type UIHints,
} from './GracefulDegradationEngine';

// UI performance guard
export {
  PerformanceGuard,
  throttledRAF,
  batchUpdates,
  idleCallback,
  type FrameMetrics,
  type JankEvent,
} from './PerformanceGuard';

// Group call optimization
export {
  GroupCallOptimizer,
  type ParticipantBudget,
  type GroupBudgetAllocation,
  type SpeakerDetection,
} from './GroupCallOptimizer';

// Screen share scaling
export {
  ScreenShareScaler,
  type ContentType,
  type ScreenShareProfile,
  type ScreenShareMetrics,
} from './ScreenShareScaler';

// ── Phase 9: Systems Compatibility Layer ────────────────────

// Hardware profiles & performance modes
export {
  MINIMUM_HARDWARE,
  RECOMMENDED_HARDWARE,
  getProfile,
  getAllProfiles,
  getDefaultModeForTier,
  checkMinimumRequirements,
  shouldShowHardwareWarning,
  interpolateMediaBudget,
  type HardwareSpec,
  type PerformanceMode,
  type MediaBudget,
  type RenderBudget,
  type ResourceBudget,
  type BackgroundBudget,
  type PerformanceProfile,
} from './HardwareProfiles';

// Resource governor — CPU/RAM budget enforcement
export {
  ResourceGovernor,
  type GovernorSeverity,
  type GovernorActionType,
  type GovernorAction,
  type ResourceMetrics,
} from './ResourceGovernor';

// Render optimizer — DOM/CSS cost reduction
export {
  RenderOptimizer,
  type RenderMetrics,
  type DOMComplexityWarning,
} from './RenderOptimizer';

// Media budget controller — audio-priority allocation
export {
  MediaBudgetController,
  type MediaAllocation,
  type AudioAllocation,
  type VideoAllocation,
  type ScreenShareAllocation,
  type IncomingVideoPolicy,
} from './MediaBudgetController';

// Background throttler — idle/background suppression
export {
  BackgroundThrottler,
  type AppVisibilityState,
  type ThrottlePolicy,
} from './BackgroundThrottler';

// Auto performance manager — unified orchestrator
export {
  AutoPerformanceManager,
  autoPerformanceManager,
  type PerformanceStatus,
  type PerformanceEvent,
} from './AutoPerformanceManager';
