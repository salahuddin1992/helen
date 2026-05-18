/**
 * capacity/ — Phase 11: Capacity Planning System
 *
 * ┌──────────────────────────────────────────────────────────────────────────┐
 * │                   CommClient Capacity Planning Architecture              │
 * │                                                                          │
 * │  ┌─────────────────────┐       ┌────────────────────────┐               │
 * │  │ DeviceCapability     │       │ ResourceGovernor       │               │
 * │  │ Detector (Phase 9)   │──────▶│ (Phase 9)              │               │
 * │  │ [tier: DeviceTier]   │       │ [severity: 0-4]        │               │
 * │  └──────────┬──────────┘       └────────────┬───────────┘               │
 * │             │                                │                           │
 * │             ▼                                ▼                           │
 * │  ┌─────────────────────┐       ┌────────────────────────┐               │
 * │  │ CapacityModel        │       │ QualityDowngradeRules  │               │
 * │  │ • PCClass profiles   │       │ • 9 ordered rules      │               │
 * │  │ • HW reference specs │       │ • Trigger conditions   │               │
 * │  │ • Resource ceilings  │       │ • Action chains        │               │
 * │  │ • Operation limits   │       │ • Timing config        │               │
 * │  │ • CPU cost estimator │       │ • Upgrade stability    │               │
 * │  └──────────┬──────────┘       └────────────┬───────────┘               │
 * │             │                                │                           │
 * │             ▼                                ▼                           │
 * │  ┌─────────────────────┐       ┌────────────────────────┐               │
 * │  │ CallCapacityLimits   │       │ SystemPolicy           │◀── SINGLE    │
 * │  │ • Per-tier limits    │──────▶│ • checkOneToOneCall()  │   POINT OF   │
 * │  │ • Quality presets    │       │ • checkGroupCallJoin() │   TRUTH      │
 * │  │ • Hard/soft maxes    │       │ • checkScreenShare()   │               │
 * │  │ • Degrade orders     │       │ • getCurrentQuality()  │               │
 * │  │ • Screen share rules │       │ • feedMetrics()        │               │
 * │  └─────────────────────┘       │ • event subscription   │               │
 * │                                 └────────────┬───────────┘               │
 * │  ┌─────────────────────┐                     │                           │
 * │  │ CapacityProfile      │                     ▼                           │
 * │  │ Defaults             │       ┌────────────────────────┐               │
 * │  │ • Video defaults     │       │ CallEngine /           │               │
 * │  │ • Audio defaults     │──────▶│ GroupCallManager /     │               │
 * │  │ • UI toggles         │       │ ScreenShareEngine      │               │
 * │  │ • Background policy  │       │ (existing services)    │               │
 * │  │ • Resource budgets   │       └────────────────────────┘               │
 * │  │ • WebRTC constraints │                                                │
 * │  └─────────────────────┘                                                │
 * └──────────────────────────────────────────────────────────────────────────┘
 *
 * Data flow:
 *   1. DeviceCapabilityDetector determines tier on startup
 *   2. CapacityModel maps tier → PCClass → resource ceilings + operation limits
 *   3. CallCapacityLimits provides concrete per-tier limits for each call type
 *   4. CapacityProfileDefaults provides recommended starting settings
 *   5. SystemPolicy reads limits + rules, accepts live metric feeds
 *   6. SystemPolicy evaluates QualityDowngradeRules every 3 seconds
 *   7. CallEngine/GroupCallManager/ScreenShareEngine call SystemPolicy
 *      before any call initiation, participant join, or screen share start
 *   8. SystemPolicy emits events for UI notification system
 */

// ── CapacityModel ───────────────────────────────────────────
export {
  type PCClass,
  type HardwareReference,
  type ResourceCeiling,
  type OperationLimits,
  type PCClassProfile,
  getCapacityProfile,
  getAllCapacityProfiles,
  tierToPCClass,
  getLimitsForTier,
  getResourcesForTier,
  estimateCallCpuCost,
  computeMaxVideoParticipants,
} from './CapacityModel';

// ── CallCapacityLimits ──────────────────────────────────────
export {
  type QualitySpec,
  type CallLimitSet,
  type ScreenShareLimitSet,
  type TierCapacityLimits,
  getCapacityLimits,
  isCallAllowed,
  canAddParticipant,
  canScreenShare,
  getQualityForScenario,
} from './CallCapacityLimits';

// ── QualityDowngradeRules ───────────────────────────────────
export {
  type DowngradeAction,
  type UpgradeAction,
  type DowngradeRule,
  type DowngradeTrigger,
  type DowngradeTimingConfig,
  type ActiveRule,
  type EvaluationContext,
  DOWNGRADE_TIMING,
  DOWNGRADE_RULES,
  evaluateRules,
  findExpiredRules,
  getDegradationChain,
} from './QualityDowngradeRules';

// ── SystemPolicy ────────────────────────────────────────────
export {
  type PolicyDecision,
  type PolicyCheckResult,
  type PolicyEvent,
  type PolicyState,
  SystemPolicy,
  systemPolicy,
} from './SystemPolicy';

// ── CapacityProfileDefaults ─────────────────────────────────
export {
  type DefaultVideoConfig,
  type DefaultAudioConfig,
  type DefaultScreenShareConfig,
  type DefaultUIConfig,
  type DefaultBackgroundConfig,
  type DefaultResourceBudget,
  type CapacityProfileDefault,
  getProfileDefaults,
  getAllProfileDefaults,
  getDefaultVideoConstraints,
  getDefaultGroupVideoConstraints,
  getDefaultAudioConstraints,
  getDefaultScreenShareConstraints,
  getUICustomProperties,
  getProfileComparisonTable,
} from './CapacityProfileDefaults';
