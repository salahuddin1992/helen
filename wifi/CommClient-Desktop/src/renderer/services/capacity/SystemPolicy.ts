/**
 * SystemPolicy.ts — Capacity enforcement engine.
 *
 * The centralized policy engine that:
 *   1. Reads device tier from DeviceCapabilityDetector
 *   2. Loads capacity limits from CallCapacityLimits
 *   3. Evaluates downgrade rules from QualityDowngradeRules
 *   4. Enforces hard limits (blocks operations that exceed capacity)
 *   5. Applies soft limits (warns and degrades quality)
 *   6. Tracks active degradation rules and handles upgrade recovery
 *   7. Emits policy events for UI notifications
 *
 * This is the SINGLE POINT OF TRUTH for "can the user do X?"
 * All call initiation, participant joining, and screen sharing flows
 * check SystemPolicy before proceeding.
 *
 * Integration:
 *   - Consumed by CallEngine, GroupCallManager, ScreenShareEngine
 *   - Fed by ResourceGovernor, PerformanceGuard, NetworkQualityMonitor
 *   - Emits events consumed by UI notification system
 */

import type { DeviceTier } from '../performance/DeviceCapabilityDetector';
import type { GovernorSeverity } from '../performance/ResourceGovernor';
import {
  getCapacityLimits,
  canAddParticipant,
  canScreenShare,
  getQualityForScenario,
  type TierCapacityLimits,
  type QualitySpec,
  type CallLimitSet,
} from './CallCapacityLimits';
import {
  evaluateRules,
  findExpiredRules,
  DOWNGRADE_TIMING,
  DOWNGRADE_RULES,
  type EvaluationContext,
  type DowngradeRule,
  type ActiveRule,
} from './QualityDowngradeRules';
import { tierToPCClass, type PCClass } from './CapacityModel';

// ── Types ───────────────────────────────────────────────────

export type PolicyDecision = 'allow' | 'allow_with_warning' | 'block';

export interface PolicyCheckResult {
  decision: PolicyDecision;
  /** i18n message key for the user */
  messageKey: string;
  /** Current quality that would be applied */
  quality?: { self: QualitySpec; peer: QualitySpec };
  /** Remaining capacity (e.g., "3 more participants possible") */
  remainingCapacity?: number;
  /** Active downgrade rules affecting this decision */
  activeDowngrades: string[];
}

export interface PolicyEvent {
  type:
    | 'downgrade_applied'
    | 'upgrade_applied'
    | 'hard_limit_reached'
    | 'soft_limit_reached'
    | 'capacity_warning'
    | 'capacity_cleared';
  ruleId: string;
  messageKey: string;
  severity: GovernorSeverity;
  timestamp: number;
}

export interface PolicyState {
  /** Current device tier */
  tier: DeviceTier;
  /** Current PC class */
  pcClass: PCClass;
  /** Active capacity limits */
  limits: TierCapacityLimits;
  /** Currently active downgrade rules */
  activeRules: ActiveRule[];
  /** Currently active downgrade actions */
  activeActions: Set<string>;
  /** Current governor severity */
  severity: GovernorSeverity;
  /** Whether the system is in emergency mode */
  emergencyMode: boolean;
  /** Last evaluation timestamp */
  lastEvaluation: number;
  /** Count of consecutive downgrades */
  consecutiveDowngrades: number;
  /** Whether downgrades are paused (after rapid sequence) */
  downgradesPaused: boolean;
}

type PolicyEventCallback = (event: PolicyEvent) => void;

// ── SystemPolicy ────────────────────────────────────────────

export class SystemPolicy {
  private _tier: DeviceTier = 'medium';
  private _pcClass: PCClass = 'normal';
  private _limits: TierCapacityLimits;
  private _activeRules: ActiveRule[] = [];
  private _activeActions = new Set<string>();
  private _severity: GovernorSeverity = 0;
  private _emergencyMode = false;
  private _listeners: PolicyEventCallback[] = [];
  private _destroyed = false;

  // Evaluation state
  private _evalTimer: ReturnType<typeof setInterval> | null = null;
  private _lastDowngradeTime = 0;
  private _consecutiveDowngrades = 0;
  private _downgradesPaused = false;
  private _pauseTimer: ReturnType<typeof setTimeout> | null = null;

  // Current metrics (fed externally)
  private _cpuPressure = 0;
  private _fps = 60;
  private _heapRatio = 0;
  private _participantCount = 0;
  private _videoStreamCount = 0;
  private _networkQuality: EvaluationContext['networkQuality'] = 'excellent';

  constructor(tier: DeviceTier = 'medium') {
    this._tier = tier;
    this._pcClass = tierToPCClass(tier);
    this._limits = getCapacityLimits(tier);
  }

  // ── Lifecycle ─────────────────────────────────────────────

  start(): void {
    if (this._destroyed) return;

    // Start periodic evaluation
    this._evalTimer = setInterval(
      () => this._evaluate(),
      DOWNGRADE_TIMING.evaluationIntervalMs,
    );

    // Apply always-on rules for the current tier
    this._applyAlwaysOnRules();
  }

  stop(): void {
    if (this._evalTimer) {
      clearInterval(this._evalTimer);
      this._evalTimer = null;
    }
    if (this._pauseTimer) {
      clearTimeout(this._pauseTimer);
      this._pauseTimer = null;
    }
  }

  destroy(): void {
    this._destroyed = true;
    this.stop();
    this._listeners = [];
    this._activeRules = [];
    this._activeActions.clear();
  }

  // ── Configuration ─────────────────────────────────────────

  setTier(tier: DeviceTier): void {
    this._tier = tier;
    this._pcClass = tierToPCClass(tier);
    this._limits = getCapacityLimits(tier);

    // Reset and reapply always-on rules
    this._activeRules = [];
    this._activeActions.clear();
    this._applyAlwaysOnRules();
  }

  // ── Metric Feeds ──────────────────────────────────────────

  feedSeverity(severity: GovernorSeverity): void {
    this._severity = severity;
    this._emergencyMode = severity >= 4;
  }

  feedCpuPressure(pressure: number): void {
    this._cpuPressure = pressure;
  }

  feedFps(fps: number): void {
    this._fps = fps;
  }

  feedHeapRatio(ratio: number): void {
    this._heapRatio = ratio;
  }

  feedParticipantCount(count: number): void {
    this._participantCount = count;
  }

  feedVideoStreamCount(count: number): void {
    this._videoStreamCount = count;
  }

  feedNetworkQuality(quality: EvaluationContext['networkQuality']): void {
    this._networkQuality = quality;
  }

  // ── Policy Checks (the "Can I do X?" API) ─────────────────

  /**
   * Check if a 1:1 call can be initiated.
   */
  checkOneToOneCall(type: 'audio' | 'video'): PolicyCheckResult {
    if (this._emergencyMode) {
      return {
        decision: 'block',
        messageKey: 'capacity.emergency_no_new_calls',
        activeDowngrades: this._getActiveRuleIds(),
      };
    }

    const callType = type === 'audio' ? 'oneToOneAudio' : 'oneToOneVideo';
    const limits = this._limits[callType];

    if (!limits.allowed) {
      return {
        decision: 'block',
        messageKey: 'capacity.call_type_not_supported',
        activeDowngrades: this._getActiveRuleIds(),
      };
    }

    const quality = getQualityForScenario(this._tier, {
      callType, participantCount: 2, isScreenSharing: false,
    });

    return {
      decision: 'allow',
      messageKey: '',
      quality,
      activeDowngrades: this._getActiveRuleIds(),
    };
  }

  /**
   * Check if a participant can join a group call.
   */
  checkGroupCallJoin(
    type: 'audio' | 'video',
    currentParticipants: number,
  ): PolicyCheckResult {
    if (this._emergencyMode) {
      return {
        decision: 'block',
        messageKey: 'capacity.emergency_no_new_calls',
        activeDowngrades: this._getActiveRuleIds(),
      };
    }

    const callType = type === 'audio' ? 'groupAudio' : 'groupVideo';
    const check = canAddParticipant(this._tier, callType, currentParticipants);

    if (!check.allowed) {
      return {
        decision: 'block',
        messageKey: check.message,
        remainingCapacity: 0,
        activeDowngrades: this._getActiveRuleIds(),
      };
    }

    const limits = this._limits[callType];
    const quality = getQualityForScenario(this._tier, {
      callType, participantCount: currentParticipants + 1, isScreenSharing: false,
    });

    const decision: PolicyDecision = check.atSoftLimit ? 'allow_with_warning' : 'allow';

    return {
      decision,
      messageKey: check.message,
      quality,
      remainingCapacity: limits.hardMax - currentParticipants - 1,
      activeDowngrades: this._getActiveRuleIds(),
    };
  }

  /**
   * Check if screen sharing can be started.
   */
  checkScreenShare(direction: 'send' | 'receive', isInVideoCall: boolean): PolicyCheckResult {
    if (this._emergencyMode) {
      return {
        decision: 'block',
        messageKey: 'capacity.emergency_no_screenshare',
        activeDowngrades: this._getActiveRuleIds(),
      };
    }

    const check = canScreenShare(this._tier, direction, isInVideoCall);

    return {
      decision: check.allowed ? 'allow' : 'block',
      messageKey: check.message,
      activeDowngrades: this._getActiveRuleIds(),
    };
  }

  /**
   * Get the current quality specs for an active call.
   */
  getCurrentQuality(
    callType: 'oneToOneAudio' | 'oneToOneVideo' | 'groupAudio' | 'groupVideo',
    participantCount: number,
    isScreenSharing: boolean,
  ): { self: QualitySpec; peer: QualitySpec } {
    return getQualityForScenario(this._tier, {
      callType, participantCount, isScreenSharing,
    });
  }

  // ── State Access ──────────────────────────────────────────

  getState(): PolicyState {
    return {
      tier: this._tier,
      pcClass: this._pcClass,
      limits: this._limits,
      activeRules: [...this._activeRules],
      activeActions: new Set(this._activeActions),
      severity: this._severity,
      emergencyMode: this._emergencyMode,
      lastEvaluation: Date.now(),
      consecutiveDowngrades: this._consecutiveDowngrades,
      downgradesPaused: this._downgradesPaused,
    };
  }

  isEmergencyMode(): boolean { return this._emergencyMode; }
  getActiveActions(): Set<string> { return new Set(this._activeActions); }

  // ── Event Subscription ────────────────────────────────────

  on(cb: PolicyEventCallback): () => void {
    this._listeners.push(cb);
    return () => {
      this._listeners = this._listeners.filter(l => l !== cb);
    };
  }

  // ── Internal: Evaluation Loop ─────────────────────────────

  private _evaluate(): void {
    if (this._destroyed) return;

    const context: EvaluationContext = {
      tier: this._tier,
      severity: this._severity,
      cpuPressure: this._cpuPressure,
      fps: this._fps,
      heapRatio: this._heapRatio,
      participantCount: this._participantCount,
      videoStreamCount: this._videoStreamCount,
      networkQuality: this._networkQuality,
    };

    // Find newly triggered rules
    const triggered = evaluateRules(context);
    const newRules = triggered.filter(
      rule => !this._activeRules.some(ar => ar.ruleId === rule.id)
    );

    // Find expired rules (conditions no longer met + stability passed)
    const expiredIds = findExpiredRules(this._activeRules, context);

    // Apply new downgrades
    if (!this._downgradesPaused) {
      for (const rule of newRules) {
        this._activateRule(rule);
      }
    }

    // Apply upgrades (deactivate expired rules)
    for (const ruleId of expiredIds) {
      this._deactivateRule(ruleId);
    }
  }

  private _activateRule(rule: DowngradeRule): void {
    const now = Date.now();

    // Respect minimum interval
    if (now - this._lastDowngradeTime < DOWNGRADE_TIMING.minDowngradeIntervalMs) return;

    // Check cooldown
    const existing = this._activeRules.find(ar => ar.ruleId === rule.id);
    if (existing) return;

    // Apply actions
    for (const action of rule.actions) {
      this._activeActions.add(action);
    }

    this._activeRules.push({
      ruleId: rule.id,
      activatedAt: now,
      lastTriggeredAt: now,
      currentActionIndex: rule.actions.length - 1,
    });

    this._lastDowngradeTime = now;
    this._consecutiveDowngrades++;

    // Check for rapid downgrade pause
    if (this._consecutiveDowngrades >= DOWNGRADE_TIMING.maxConsecutiveDowngrades) {
      this._downgradesPaused = true;
      this._pauseTimer = setTimeout(() => {
        this._downgradesPaused = false;
        this._consecutiveDowngrades = 0;
      }, DOWNGRADE_TIMING.downgradePauseMs);
    }

    // Emit event
    if (rule.notificationKey) {
      this._emit({
        type: 'downgrade_applied',
        ruleId: rule.id,
        messageKey: rule.notificationKey,
        severity: this._severity,
        timestamp: now,
      });
    }
  }

  private _deactivateRule(ruleId: string): void {
    const rule = DOWNGRADE_RULES.find(r => r.id === ruleId);
    if (!rule) return;

    // Remove actions (only if no other active rule requires them)
    for (const action of rule.actions) {
      const stillNeeded = this._activeRules.some(
        ar => ar.ruleId !== ruleId &&
        DOWNGRADE_RULES.find(r => r.id === ar.ruleId)?.actions.includes(action as any)
      );
      if (!stillNeeded) {
        this._activeActions.delete(action);
      }
    }

    this._activeRules = this._activeRules.filter(ar => ar.ruleId !== ruleId);
    this._consecutiveDowngrades = Math.max(0, this._consecutiveDowngrades - 1);

    this._emit({
      type: 'upgrade_applied',
      ruleId,
      messageKey: 'capacity.quality_restored',
      severity: this._severity,
      timestamp: Date.now(),
    });
  }

  private _applyAlwaysOnRules(): void {
    for (const rule of DOWNGRADE_RULES) {
      if (
        rule.applicableTiers.includes(this._tier) &&
        Object.keys(rule.trigger).length === 0
      ) {
        for (const action of rule.actions) {
          this._activeActions.add(action);
        }
        this._activeRules.push({
          ruleId: rule.id,
          activatedAt: Date.now(),
          lastTriggeredAt: Date.now(),
          currentActionIndex: rule.actions.length - 1,
        });
      }
    }
  }

  private _getActiveRuleIds(): string[] {
    return this._activeRules.map(ar => ar.ruleId);
  }

  private _emit(event: PolicyEvent): void {
    for (const cb of this._listeners) {
      try { cb(event); } catch {}
    }
  }
}

// ── Singleton ───────────────────────────────────────────────

export const systemPolicy = new SystemPolicy();
