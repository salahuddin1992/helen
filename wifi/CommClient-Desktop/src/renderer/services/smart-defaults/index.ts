/**
 * smart-defaults/ — Phase 16: Smart Defaults & Adaptive Configuration
 *
 * ┌──────────────────────────────────────────────────────────────────────────┐
 * │            CommClient Smart Defaults Architecture                        │
 * │                                                                          │
 * │  ┌──────────────────────────────────────────────────────────────────┐   │
 * │  │                    SmartDefaultRules.ts                          │   │
 * │  │                                                                  │   │
 * │  │  • Environment detection (HW + network + devices + power)        │   │
 * │  │  • Quality profile matrix (DeviceTier × NetworkTier)             │   │
 * │  │  • Scenario adjustments (8 call scenarios with multipliers)      │   │
 * │  │  • LAN RTT probing → NetworkTier classification                  │   │
 * │  │  • User override tracking (user choices are sacred)              │   │
 * │  │  • Startup initialization sequence                               │   │
 * │  │  • Re-evaluation triggers (device/network/call/power/resume)     │   │
 * │  └──────────────────────────────┬───────────────────────────────────┘   │
 * │                                 │                                       │
 * │                    ┌────────────┼────────────┐                          │
 * │                    ▼            ▼            ▼                          │
 * │  ┌──────────────────┐ ┌─────────────────┐ ┌─────────────────────────┐ │
 * │  │ DeviceSelection  │ │ MediaQuality    │ │ SafeFallbackDefaults   │ │
 * │  │ Engine.ts        │ │ AutoSelector.ts │ │ .ts                    │ │
 * │  │                  │ │                 │ │                         │ │
 * │  │ • 12 heuristic   │ │ • 5 quality     │ │ • 4-layer fallback     │ │
 * │  │   scoring signals│ │   profiles      │ │   chain                │ │
 * │  │ • Blacklists     │ │   (minimal→     │ │ • Per-tier complete    │ │
 * │  │   (virtual devs) │ │    ultra)       │ │   defaults table       │ │
 * │  │ • Device history │ │ • getUserMedia  │ │ • Emergency audio      │ │
 * │  │   & memory       │ │   constraint    │ │   constraints          │ │
 * │  │ • Hot-swap       │ │   builder       │ │ • Progressive media    │ │
 * │  │   detection      │ │ • Tier caps     │ │   fallback (4 levels)  │ │
 * │  │ • Category-aware │ │ • Scenario      │ │ • Tier detection       │ │
 * │  │   (USB>headset>  │ │   multipliers   │ │   fallback             │ │
 * │  │    builtin)      │ │ • Bitrate &     │ │ • Settings validation  │ │
 * │  │ • Confidence     │ │   display media │ │   & sanitization       │ │
 * │  │   scoring        │ │   converters    │ │ • Audio-first policy   │ │
 * │  └──────────────────┘ └─────────────────┘ └─────────────────────────┘ │
 * │                                                                        │
 * │  ┌──────────────────────────────────────────────────────────────────┐   │
 * │  │                    Integration Points                            │   │
 * │  │                                                                  │   │
 * │  │  SmartDefaults.ts (existing)                                     │   │
 * │  │    → Device scoring extended by DeviceSelectionEngine            │   │
 * │  │                                                                  │   │
 * │  │  CapacityModel.ts (Phase 11)                                     │   │
 * │  │    → DeviceTier/PCClass feeds into quality profile selection     │   │
 * │  │                                                                  │   │
 * │  │  CapacityProfileDefaults.ts (Phase 11)                           │   │
 * │  │    → Per-tier media defaults referenced as tier layer            │   │
 * │  │                                                                  │   │
 * │  │  CallCapacityLimits.ts (Phase 11)                                │   │
 * │  │    → Hard caps enforced during constraint clamping               │   │
 * │  │                                                                  │   │
 * │  │  QualityDowngradeRules.ts (Phase 11)                             │   │
 * │  │    → Runtime degradation works WITH smart defaults               │   │
 * │  │                                                                  │   │
 * │  │  SystemPolicy.ts (Phase 11)                                      │   │
 * │  │    → Policy check uses smart-default quality as baseline         │   │
 * │  │                                                                  │   │
 * │  │  MediaDeviceManager.ts (existing)                                │   │
 * │  │    → DeviceSelectionEngine enriches its device list              │   │
 * │  │                                                                  │   │
 * │  │  settings.store.ts (existing)                                    │   │
 * │  │    → Smart defaults write to settings store at startup           │   │
 * │  │                                                                  │   │
 * │  │  AutoPerformanceManager.ts (existing)                            │   │
 * │  │    → Initialized after smart defaults, consumes detected tier    │   │
 * │  │                                                                  │   │
 * │  │  app-mode.store.ts (Phase 7)                                     │   │
 * │  │    → Simple Mode hides quality controls                          │   │
 * │  │    → Advanced Mode exposes manual overrides                      │   │
 * │  └──────────────────────────────────────────────────────────────────┘   │
 * └──────────────────────────────────────────────────────────────────────────┘
 */

// ── SmartDefaultRules ───────────────────────────────────────────
export {
  type DeviceTier,
  type PCClass,
  type NetworkTier,
  type QualityProfile,
  type CallScenario,
  type SettingOrigin,
  type ResolvedSetting,
  type EnvironmentSnapshot,
  type SmartDefaultRule,
  type ScenarioAdjustment,
  type SmartDefaultsResult,
  type ReevaluationTrigger,
  QUALITY_PROFILE_MATRIX,
  SCENARIO_ADJUSTMENTS,
  REEVALUATION_TRIGGERS,
  resolveQualityProfile,
  detectCallScenario,
  classifyNetworkTier,
  probeLanRTT,
  detectEnvironment,
  isUserExplicitSetting,
  markUserExplicit,
  clearUserExplicit,
  clearAllUserOverrides,
  initializeSmartDefaults,
} from './SmartDefaultRules';

// ── DeviceSelectionEngine ───────────────────────────────────────
export {
  type DeviceKind,
  type ScoredDevice,
  type DeviceCategory,
  type DeviceSelection,
  type FullDeviceRecommendation,
  type HotSwapResult,
  selectAllDevices,
  handleDeviceChange,
  recordDeviceSuccess,
  recordDeviceFailure,
} from './DeviceSelectionEngine';

// ── MediaQualityAutoSelector ────────────────────────────────────
export {
  type VideoConstraints,
  type AudioConstraints,
  type ScreenShareConstraints,
  type ResolvedMediaConstraints,
  resolveMediaConstraints,
  resolveMediaConstraintsFromEnv,
  toGetUserMediaConstraints,
  toBitrateParameters,
  toDisplayMediaConstraints,
  getProfileSummaries,
} from './MediaQualityAutoSelector';

// ── SafeFallbackDefaults ────────────────────────────────────────
export {
  type TierDefaults,
  getDefaultsForTier,
  getSafestDefaults,
  resolveSetting,
  resolveAllSettings,
  EMERGENCY_AUDIO_CONSTRAINTS,
  EMERGENCY_VIDEO_CONSTRAINTS,
  getMediaWithFallback,
  detectTierFallback,
  validateSettings,
} from './SafeFallbackDefaults';
