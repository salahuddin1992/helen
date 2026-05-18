/**
 * QualityDowngradeRules.ts — Automatic degradation triggers and actions.
 *
 * Defines the complete rules engine for quality downgrade decisions:
 *   - What triggers a downgrade (CPU, memory, FPS, network, participant count)
 *   - What gets downgraded first (priority-ordered action chains)
 *   - How fast downgrades happen (immediate vs gradual)
 *   - When upgrades are safe (stability requirements)
 *   - What the user sees during downgrades (notification keys)
 *
 * Design principles:
 *   1. AUDIO IS SACRED — never downgrade audio unless survival mode
 *   2. Prefer invisible downgrades — reduce peer quality before self quality
 *   3. Gradual not sudden — step through resolution/FPS before disabling
 *   4. Fast down, slow up — react quickly to pressure, recover cautiously
 *   5. Respect hard limits — never exceed CapacityModel ceilings
 *   6. User awareness — always notify when quality changes
 *
 * Integrates with:
 *   - ResourceGovernor (severity signals 0-4)
 *   - GracefulDegradationEngine (levels 0-5)
 *   - MediaBudgetController (audio-priority allocation)
 *   - GroupCallResourceManager (per-participant budgets)
 */

import type { GovernorSeverity } from '../performance/ResourceGovernor';
import type { DeviceTier } from '../performance/DeviceCapabilityDetector';

// ── Types ───────────────────────────────────────────────────

export type DowngradeAction =
  | 'reduce_peer_fps'
  | 'reduce_peer_resolution'
  | 'reduce_self_fps'
  | 'reduce_self_resolution'
  | 'disable_peer_video'
  | 'disable_self_video'
  | 'reduce_screenshare_fps'
  | 'disable_screenshare'
  | 'reduce_audio_bitrate'
  | 'reduce_animations'
  | 'disable_effects'
  | 'enable_store_batching'
  | 'disable_typing_indicators'
  | 'reduce_avatar_quality';

export type UpgradeAction =
  | 'restore_peer_fps'
  | 'restore_peer_resolution'
  | 'restore_self_fps'
  | 'restore_self_resolution'
  | 'enable_peer_video'
  | 'enable_self_video'
  | 'restore_screenshare_fps'
  | 'enable_screenshare'
  | 'restore_audio_bitrate'
  | 'restore_animations'
  | 'enable_effects'
  | 'disable_store_batching'
  | 'enable_typing_indicators'
  | 'restore_avatar_quality';

export interface DowngradeRule {
  /** Unique ID for the rule */
  id: string;
  /** Human-readable description */
  description: string;
  /** Trigger condition */
  trigger: DowngradeTrigger;
  /** Ordered list of actions to execute */
  actions: DowngradeAction[];
  /** Reverse actions for upgrade */
  reverseActions: UpgradeAction[];
  /** i18n key for user notification */
  notificationKey: string;
  /** Minimum time before this rule can trigger again (ms) */
  cooldownMs: number;
  /** Whether this is a hard requirement or can be overridden */
  mandatory: boolean;
  /** Applies to which device tiers */
  applicableTiers: DeviceTier[];
}

export interface DowngradeTrigger {
  /** Resource governor severity threshold (trigger when >= this) */
  minSeverity?: GovernorSeverity;
  /** CPU pressure threshold (0-1, trigger when >=) */
  cpuPressure?: number;
  /** FPS threshold (trigger when <=) */
  maxFps?: number;
  /** Heap usage ratio (trigger when >=) */
  heapRatio?: number;
  /** Participant count threshold (trigger when >=) */
  participantCount?: number;
  /** Active video stream count (trigger when >=) */
  videoStreamCount?: number;
  /** Network quality (trigger when worse or equal) */
  networkQuality?: 'fair' | 'poor' | 'critical';
}

export interface DowngradeTimingConfig {
  /** Time to wait before applying downgrade (ms) */
  downgradeDelayMs: number;
  /** Time of sustained good conditions before upgrading (ms) */
  upgradeStabilityMs: number;
  /** Re-evaluation interval (ms) */
  evaluationIntervalMs: number;
  /** Minimum time between consecutive downgrades (ms) */
  minDowngradeIntervalMs: number;
  /** Maximum consecutive downgrades before pausing */
  maxConsecutiveDowngrades: number;
  /** Pause duration after max consecutive downgrades (ms) */
  downgradePauseMs: number;
}

export interface ActiveRule {
  ruleId: string;
  activatedAt: number;
  lastTriggeredAt: number;
  currentActionIndex: number;
}

// ── Constants ───────────────────────────────────────────────

/** Timing configuration */
export const DOWNGRADE_TIMING: DowngradeTimingConfig = {
  downgradeDelayMs: 2_000,        // React within 2 seconds
  upgradeStabilityMs: 15_000,     // 15 seconds of good conditions
  evaluationIntervalMs: 3_000,    // Check every 3 seconds
  minDowngradeIntervalMs: 5_000,  // At most 1 downgrade per 5 seconds
  maxConsecutiveDowngrades: 4,    // Pause after 4 rapid downgrades
  downgradePauseMs: 10_000,       // 10 second pause between flurries
};

// ── Rule Definitions ────────────────────────────────────────

/**
 * Complete ordered rule set. Rules are evaluated in order;
 * the first matching rule's actions are applied.
 */
export const DOWNGRADE_RULES: DowngradeRule[] = [
  // ── RULE 1: Emergency — everything goes to audio only ───
  {
    id: 'emergency_audio_only',
    description: 'Emergency: CPU/memory critical, switch to audio-only to prevent crash',
    trigger: {
      minSeverity: 4,
    },
    actions: [
      'disable_peer_video',
      'disable_self_video',
      'disable_screenshare',
      'disable_effects',
      'reduce_animations',
      'enable_store_batching',
      'disable_typing_indicators',
    ],
    reverseActions: [
      'enable_peer_video',
      'enable_self_video',
      'enable_screenshare',
      'enable_effects',
      'restore_animations',
      'disable_store_batching',
      'enable_typing_indicators',
    ],
    notificationKey: 'capacity.emergency_audio_only',
    cooldownMs: 30_000,
    mandatory: true,
    applicableTiers: ['minimal', 'low', 'medium', 'high'],
  },

  // ── RULE 2: Heavy pressure — reduce to minimal video ────
  {
    id: 'heavy_pressure_minimal_video',
    description: 'Heavy CPU pressure: reduce video to minimum, disable effects',
    trigger: {
      minSeverity: 3,
      cpuPressure: 0.8,
    },
    actions: [
      'reduce_peer_resolution',
      'reduce_peer_fps',
      'reduce_self_resolution',
      'reduce_self_fps',
      'reduce_screenshare_fps',
      'disable_effects',
      'reduce_animations',
    ],
    reverseActions: [
      'restore_peer_resolution',
      'restore_peer_fps',
      'restore_self_resolution',
      'restore_self_fps',
      'restore_screenshare_fps',
      'enable_effects',
      'restore_animations',
    ],
    notificationKey: 'capacity.heavy_pressure',
    cooldownMs: 15_000,
    mandatory: true,
    applicableTiers: ['minimal', 'low', 'medium', 'high'],
  },

  // ── RULE 3: Too many video participants ──────────────────
  {
    id: 'too_many_video_streams',
    description: 'Too many video streams: disable non-speaker video',
    trigger: {
      videoStreamCount: 5,
    },
    actions: [
      'disable_peer_video',
      'reduce_self_resolution',
    ],
    reverseActions: [
      'enable_peer_video',
      'restore_self_resolution',
    ],
    notificationKey: 'capacity.too_many_videos',
    cooldownMs: 10_000,
    mandatory: false,
    applicableTiers: ['minimal', 'low', 'medium'],
  },

  // ── RULE 4: Moderate pressure — reduce quality ──────────
  {
    id: 'moderate_pressure_reduce',
    description: 'Moderate CPU/memory pressure: reduce peer video quality',
    trigger: {
      minSeverity: 2,
      cpuPressure: 0.6,
    },
    actions: [
      'reduce_peer_fps',
      'reduce_peer_resolution',
      'reduce_screenshare_fps',
      'reduce_avatar_quality',
    ],
    reverseActions: [
      'restore_peer_fps',
      'restore_peer_resolution',
      'restore_screenshare_fps',
      'restore_avatar_quality',
    ],
    notificationKey: 'capacity.reducing_quality',
    cooldownMs: 10_000,
    mandatory: false,
    applicableTiers: ['minimal', 'low', 'medium', 'high'],
  },

  // ── RULE 5: Low FPS — reduce rendering cost ─────────────
  {
    id: 'low_fps_render_reduce',
    description: 'Low UI FPS: disable animations and visual effects',
    trigger: {
      maxFps: 20,
    },
    actions: [
      'reduce_animations',
      'disable_effects',
      'enable_store_batching',
    ],
    reverseActions: [
      'restore_animations',
      'enable_effects',
      'disable_store_batching',
    ],
    notificationKey: 'capacity.reducing_effects',
    cooldownMs: 10_000,
    mandatory: false,
    applicableTiers: ['minimal', 'low', 'medium', 'high'],
  },

  // ── RULE 6: Large group — preemptive reduction ──────────
  {
    id: 'large_group_preemptive',
    description: 'Large group: preemptively reduce video to prevent overload',
    trigger: {
      participantCount: 6,
    },
    actions: [
      'reduce_peer_resolution',
      'reduce_peer_fps',
    ],
    reverseActions: [
      'restore_peer_resolution',
      'restore_peer_fps',
    ],
    notificationKey: 'capacity.large_group_optimizing',
    cooldownMs: 0,
    mandatory: false,
    applicableTiers: ['minimal', 'low', 'medium'],
  },

  // ── RULE 7: Network degraded ────────────────────────────
  {
    id: 'network_poor',
    description: 'Poor network quality: reduce bitrates and disable video',
    trigger: {
      networkQuality: 'poor',
    },
    actions: [
      'reduce_peer_resolution',
      'reduce_self_resolution',
      'reduce_screenshare_fps',
      'reduce_audio_bitrate',
    ],
    reverseActions: [
      'restore_peer_resolution',
      'restore_self_resolution',
      'restore_screenshare_fps',
      'restore_audio_bitrate',
    ],
    notificationKey: 'capacity.network_poor',
    cooldownMs: 15_000,
    mandatory: true,
    applicableTiers: ['minimal', 'low', 'medium', 'high'],
  },

  // ── RULE 8: Memory pressure ─────────────────────────────
  {
    id: 'memory_pressure',
    description: 'High heap usage: reduce caches and batch updates',
    trigger: {
      heapRatio: 0.75,
    },
    actions: [
      'enable_store_batching',
      'reduce_avatar_quality',
      'disable_typing_indicators',
    ],
    reverseActions: [
      'disable_store_batching',
      'restore_avatar_quality',
      'enable_typing_indicators',
    ],
    notificationKey: 'capacity.memory_pressure',
    cooldownMs: 30_000,
    mandatory: false,
    applicableTiers: ['minimal', 'low', 'medium', 'high'],
  },

  // ── RULE 9: Weak device preemptive (always on for minimal) ─
  {
    id: 'weak_device_always',
    description: 'Weak device: always run with reduced effects',
    trigger: {},  // No trigger — always active for minimal tier
    actions: [
      'reduce_animations',
      'disable_effects',
      'reduce_avatar_quality',
      'disable_typing_indicators',
    ],
    reverseActions: [
      'restore_animations',
      'enable_effects',
      'restore_avatar_quality',
      'enable_typing_indicators',
    ],
    notificationKey: '',  // Silent — this is the default experience
    cooldownMs: 0,
    mandatory: true,
    applicableTiers: ['minimal'],
  },
];

// ── Rule Evaluation ─────────────────────────────────────────

export interface EvaluationContext {
  tier: DeviceTier;
  severity: GovernorSeverity;
  cpuPressure: number;
  fps: number;
  heapRatio: number;
  participantCount: number;
  videoStreamCount: number;
  networkQuality: 'excellent' | 'good' | 'fair' | 'poor' | 'critical';
}

/**
 * Evaluate which rules should be active given the current context.
 * Returns rules in priority order (first = most urgent).
 */
export function evaluateRules(context: EvaluationContext): DowngradeRule[] {
  const active: DowngradeRule[] = [];

  for (const rule of DOWNGRADE_RULES) {
    // Check tier applicability
    if (!rule.applicableTiers.includes(context.tier)) continue;

    // Check trigger conditions
    if (matchesTrigger(rule.trigger, context)) {
      active.push(rule);
    }
  }

  return active;
}

/**
 * Check if a trigger matches the current context.
 * All specified conditions must be met (AND logic).
 */
function matchesTrigger(trigger: DowngradeTrigger, ctx: EvaluationContext): boolean {
  // Empty trigger = always matches (used for always-on rules)
  const conditions = Object.keys(trigger);
  if (conditions.length === 0) return true;

  if (trigger.minSeverity !== undefined && ctx.severity < trigger.minSeverity) return false;
  if (trigger.cpuPressure !== undefined && ctx.cpuPressure < trigger.cpuPressure) return false;
  if (trigger.maxFps !== undefined && ctx.fps > trigger.maxFps) return false;
  if (trigger.heapRatio !== undefined && ctx.heapRatio < trigger.heapRatio) return false;
  if (trigger.participantCount !== undefined && ctx.participantCount < trigger.participantCount) return false;
  if (trigger.videoStreamCount !== undefined && ctx.videoStreamCount < trigger.videoStreamCount) return false;

  if (trigger.networkQuality !== undefined) {
    const qualityOrder = ['excellent', 'good', 'fair', 'poor', 'critical'];
    const triggerIdx = qualityOrder.indexOf(trigger.networkQuality);
    const contextIdx = qualityOrder.indexOf(ctx.networkQuality);
    if (contextIdx < triggerIdx) return false;
  }

  return true;
}

/**
 * Determine which rules should be deactivated (conditions no longer met).
 */
export function findExpiredRules(
  activeRules: ActiveRule[],
  context: EvaluationContext,
): string[] {
  const expired: string[] = [];

  for (const active of activeRules) {
    const rule = DOWNGRADE_RULES.find(r => r.id === active.ruleId);
    if (!rule) {
      expired.push(active.ruleId);
      continue;
    }

    // Check if the trigger conditions are no longer met
    if (!matchesTrigger(rule.trigger, context)) {
      // Check stability requirement (must stay clear for upgradeStabilityMs)
      const clearedDuration = Date.now() - active.lastTriggeredAt;
      if (clearedDuration >= DOWNGRADE_TIMING.upgradeStabilityMs) {
        expired.push(active.ruleId);
      }
    }
  }

  return expired;
}

/**
 * Get the degradation action chain for a specific call scenario.
 * Returns an ordered list of what gets downgraded first.
 */
export function getDegradationChain(
  tier: DeviceTier,
  callType: 'oneToOneAudio' | 'oneToOneVideo' | 'groupAudio' | 'groupVideo',
): string[] {
  // This is the "what breaks first" ordered list, tier-specific
  const chains: Record<DeviceTier, Record<string, string[]>> = {
    minimal: {
      oneToOneAudio: ['audio_bitrate', 'sample_rate'],
      oneToOneVideo: ['video_fps→10', 'video_res→360p', 'disable_video→audio_only'],
      groupAudio: ['audio_bitrate', 'sample_rate'],
      groupVideo: ['peer_fps→10', 'peer_res→thumbnail', 'disable_peer_video', 'self_fps→10', 'self_res→360p', 'disable_self_video'],
    },
    low: {
      oneToOneAudio: ['audio_bitrate'],
      oneToOneVideo: ['video_fps→15', 'video_res→360p', 'disable_video→audio_only'],
      groupAudio: ['audio_bitrate', 'sample_rate'],
      groupVideo: ['peer_fps→15', 'peer_res→thumbnail', 'disable_peer_video', 'self_fps→15', 'self_res→360p', 'disable_self_video'],
    },
    medium: {
      oneToOneAudio: ['audio_bitrate'],
      oneToOneVideo: ['video_fps→24', 'video_res→480p', 'video_fps→15', 'video_res→360p', 'disable_video'],
      groupAudio: ['audio_bitrate'],
      groupVideo: ['peer_fps→15', 'peer_res→small', 'peer_res→thumbnail', 'disable_non_speaker_video', 'self_res→360p', 'disable_self_video'],
    },
    high: {
      oneToOneAudio: ['audio_bitrate'],
      oneToOneVideo: ['video_fps→24', 'video_res→720p', 'video_fps→15', 'video_res→480p', 'disable_video'],
      groupAudio: ['audio_bitrate'],
      groupVideo: ['peer_fps→24', 'peer_res→small', 'peer_fps→15', 'peer_res→thumbnail', 'disable_non_speaker_video', 'self_res→480p', 'disable_self_video'],
    },
  };

  return chains[tier][callType] ?? [];
}
