/**
 * CrashRecoveryManager.ts — Crash detection, safe restart, and state recovery.
 *
 * Detects abnormal shutdowns and orchestrates recovery on next launch.
 * Works in tandem with SafeStateManager (atomic writes) and
 * SessionRecoveryEngine (session restore).
 *
 * Crash detection strategy:
 *   1. Write a "heartbeat" flag to IndexedDB every 5 seconds
 *   2. On startup, check if the flag is "running" (crash) or "clean" (normal)
 *   3. If crash detected, read the crash journal for recoverable state
 *   4. Route recovery through ErrorClassifier to determine actions
 *
 * Recovery tiers:
 * ┌────────────────────┬─────────────────────────────────────────────────┐
 * │ Tier               │ Action                                          │
 * ├────────────────────┼─────────────────────────────────────────────────┤
 * │ CLEAN              │ Normal startup — no recovery needed              │
 * │ SOFT_CRASH         │ Renderer crashed — restore session, reconnect   │
 * │ HARD_CRASH         │ Main process crash — validate DB, restore UI    │
 * │ CORRUPT            │ Data corruption — repair DB, reset config       │
 * │ UNRECOVERABLE      │ Fatal — offer clean slate with data backup      │
 * └────────────────────┴─────────────────────────────────────────────────┘
 *
 * Integration points:
 *   - AppLifecycleManager: calls initialize() on startup
 *   - SafeStateManager: reads crash journal entries
 *   - SessionRecoveryEngine: restores active sessions
 *   - ErrorClassifier: routes errors to correct recovery tier
 *   - ConnectionResilience: reconnects sockets post-recovery
 */

// ── Types ───────────────────────────────────────────────────

export type CrashTier = 'clean' | 'soft_crash' | 'hard_crash' | 'corrupt' | 'unrecoverable';

export type ShutdownReason = 'normal' | 'update' | 'user_quit' | 'crash' | 'unknown';

export interface CrashRecord {
  /** Unique crash ID */
  id: string;
  /** When the crash was detected */
  detectedAt: number;
  /** What tier of crash this is */
  tier: CrashTier;
  /** Last known app phase before crash */
  lastPhase: string;
  /** Last known route/page */
  lastRoute: string;
  /** Was the user in a call? */
  wasInCall: boolean;
  /** Active call peer IDs (if in call) */
  callPeerIds: string[];
  /** Unsent message drafts (channel → text) */
  pendingDrafts: Record<string, string>;
  /** Pending file transfers (transferId → progress%) */
  pendingTransfers: Record<string, number>;
  /** Number of consecutive crashes without clean shutdown */
  consecutiveCrashes: number;
  /** Last error message if available */
  lastError: string;
  /** Renderer uptime before crash (ms) */
  uptimeMs: number;
}

export interface RecoveryPlan {
  tier: CrashTier;
  actions: RecoveryAction[];
  userMessage: string;
  /** Whether to show recovery UI to the user */
  showRecoveryUI: boolean;
  /** Estimated recovery time (ms) */
  estimatedTimeMs: number;
}

export type RecoveryAction =
  | 'validate_database'
  | 'repair_database'
  | 'restore_config'
  | 'restore_drafts'
  | 'reconnect_socket'
  | 'rejoin_call'
  | 'resume_transfers'
  | 'clear_cache'
  | 'reset_stores'
  | 'clean_temp_files'
  | 'show_reconnect_prompt'
  | 'offer_clean_slate';

export interface RecoveryResult {
  success: boolean;
  tier: CrashTier;
  actionsExecuted: RecoveryAction[];
  actionsFailed: RecoveryAction[];
  timingMs: number;
  userNotified: boolean;
}

// ── Constants ───────────────────────────────────────────────

const HEARTBEAT_KEY = 'cc_process_heartbeat';
const CRASH_JOURNAL_KEY = 'cc_crash_journal';
const SHUTDOWN_REASON_KEY = 'cc_shutdown_reason';
const STARTUP_COUNT_KEY = 'cc_startup_count';
const CONSECUTIVE_CRASH_KEY = 'cc_consecutive_crashes';
const HEARTBEAT_INTERVAL_MS = 5_000;
const MAX_CONSECUTIVE_CRASHES = 3; // After this, offer clean slate
const CRASH_JOURNAL_MAX_ENTRIES = 10;

// ── Storage Helpers (localStorage-based for renderer) ───────

function storageGet(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}

function storageSet(key: string, value: string): void {
  try { localStorage.setItem(key, value); } catch {}
}

function storageRemove(key: string): void {
  try { localStorage.removeItem(key); } catch {}
}

// ── Singleton ───────────────────────────────────────────────

class CrashRecoveryManager {
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private startTime = Date.now();
  private currentPhase = 'startup';
  private currentRoute = '/';
  private callState: { inCall: boolean; peerIds: string[] } = { inCall: false, peerIds: [] };
  private pendingDrafts: Record<string, string> = {};
  private pendingTransfers: Record<string, number> = {};
  private lastError = '';
  private initialized = false;

  // ── Lifecycle ─────────────────────────────────────────────

  /**
   * Called once on renderer startup. Detects crash and returns recovery plan.
   */
  initialize(): RecoveryPlan {
    this.initialized = true;
    this.startTime = Date.now();

    // Step 1: Detect crash tier
    const tier = this.detectCrashTier();

    // Step 2: Read crash journal if crash detected
    let crashRecord: CrashRecord | null = null;
    if (tier !== 'clean') {
      crashRecord = this.readCrashJournal();
      this.updateConsecutiveCrashCount(tier);
    } else {
      this.resetConsecutiveCrashCount();
    }

    // Step 3: Build recovery plan
    const plan = this.buildRecoveryPlan(tier, crashRecord);

    // Step 4: Start heartbeat (marks process as "running")
    this.startHeartbeat();

    // Step 5: Increment startup count
    const startupCount = parseInt(storageGet(STARTUP_COUNT_KEY) || '0', 10);
    storageSet(STARTUP_COUNT_KEY, String(startupCount + 1));

    return plan;
  }

  /**
   * Called on normal shutdown (before app.quit / window.close).
   */
  shutdown(reason: ShutdownReason = 'normal'): void {
    this.stopHeartbeat();
    storageSet(SHUTDOWN_REASON_KEY, reason);
    storageSet(HEARTBEAT_KEY, 'clean');

    // Persist final state for crash recovery
    this.writeCrashJournal();
  }

  // ── State Tracking (call from various parts of the app) ───

  /** Update current app phase (e.g., 'ready', 'in_call', 'settings') */
  setPhase(phase: string): void {
    this.currentPhase = phase;
  }

  /** Update current route */
  setRoute(route: string): void {
    this.currentRoute = route;
  }

  /** Update call state for recovery */
  setCallState(inCall: boolean, peerIds: string[]): void {
    this.callState = { inCall, peerIds };
  }

  /** Save a message draft for recovery */
  saveDraft(channelId: string, text: string): void {
    if (text.trim()) {
      this.pendingDrafts[channelId] = text;
    } else {
      delete this.pendingDrafts[channelId];
    }
  }

  /** Track a file transfer for recovery */
  setTransferProgress(transferId: string, progress: number): void {
    if (progress >= 1) {
      delete this.pendingTransfers[transferId];
    } else {
      this.pendingTransfers[transferId] = progress;
    }
  }

  /** Record an error for crash context */
  setLastError(error: string): void {
    this.lastError = error;
  }

  /** Get the last crash record (for UI display) */
  getLastCrashRecord(): CrashRecord | null {
    return this.readCrashJournal();
  }

  /** Get consecutive crash count */
  getConsecutiveCrashes(): number {
    return parseInt(storageGet(CONSECUTIVE_CRASH_KEY) || '0', 10);
  }

  // ── Crash Detection ───────────────────────────────────────

  private detectCrashTier(): CrashTier {
    const heartbeat = storageGet(HEARTBEAT_KEY);
    const shutdownReason = storageGet(SHUTDOWN_REASON_KEY);
    const consecutive = parseInt(storageGet(CONSECUTIVE_CRASH_KEY) || '0', 10);

    // First launch ever — no previous state
    if (heartbeat === null) {
      return 'clean';
    }

    // Clean shutdown recorded
    if (heartbeat === 'clean' && (shutdownReason === 'normal' || shutdownReason === 'update' || shutdownReason === 'user_quit')) {
      return 'clean';
    }

    // Too many consecutive crashes — possible corruption
    if (consecutive >= MAX_CONSECUTIVE_CRASHES) {
      return 'unrecoverable';
    }

    // Heartbeat was "running" but no clean shutdown — crash
    if (heartbeat === 'running') {
      // Try to determine severity
      const journal = this.readCrashJournal();
      if (journal && journal.lastError.includes('database')) {
        return 'corrupt';
      }
      if (journal && journal.wasInCall) {
        return 'hard_crash'; // Call in progress means potential state mess
      }
      return 'soft_crash';
    }

    return 'clean';
  }

  // ── Recovery Plan Builder ─────────────────────────────────

  private buildRecoveryPlan(tier: CrashTier, journal: CrashRecord | null): RecoveryPlan {
    switch (tier) {
      case 'clean':
        return {
          tier: 'clean',
          actions: [],
          userMessage: '',
          showRecoveryUI: false,
          estimatedTimeMs: 0,
        };

      case 'soft_crash':
        return {
          tier: 'soft_crash',
          actions: [
            'restore_drafts',
            'reconnect_socket',
            'clean_temp_files',
          ],
          userMessage: 'resilience.soft_crash_recovered',
          showRecoveryUI: false, // Silent recovery
          estimatedTimeMs: 2_000,
        };

      case 'hard_crash':
        return {
          tier: 'hard_crash',
          actions: [
            'validate_database',
            'restore_config',
            'restore_drafts',
            'reconnect_socket',
            ...(journal?.wasInCall ? ['show_reconnect_prompt' as RecoveryAction] : []),
            'clean_temp_files',
          ],
          userMessage: 'resilience.hard_crash_recovered',
          showRecoveryUI: true,
          estimatedTimeMs: 5_000,
        };

      case 'corrupt':
        return {
          tier: 'corrupt',
          actions: [
            'repair_database',
            'restore_config',
            'clear_cache',
            'reset_stores',
            'reconnect_socket',
            'clean_temp_files',
          ],
          userMessage: 'resilience.corrupt_recovered',
          showRecoveryUI: true,
          estimatedTimeMs: 10_000,
        };

      case 'unrecoverable':
        return {
          tier: 'unrecoverable',
          actions: ['offer_clean_slate'],
          userMessage: 'resilience.unrecoverable',
          showRecoveryUI: true,
          estimatedTimeMs: 0,
        };
    }
  }

  // ── Heartbeat ─────────────────────────────────────────────

  private startHeartbeat(): void {
    storageSet(HEARTBEAT_KEY, 'running');
    storageSet(SHUTDOWN_REASON_KEY, 'unknown'); // Will be overwritten on clean shutdown

    this.heartbeatTimer = setInterval(() => {
      storageSet(HEARTBEAT_KEY, 'running');
      // Periodically update crash journal with latest state
      this.writeCrashJournal();
    }, HEARTBEAT_INTERVAL_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  // ── Crash Journal ─────────────────────────────────────────

  private writeCrashJournal(): void {
    const record: CrashRecord = {
      id: `crash-${Date.now()}`,
      detectedAt: Date.now(),
      tier: 'clean', // Will be re-evaluated on next startup
      lastPhase: this.currentPhase,
      lastRoute: this.currentRoute,
      wasInCall: this.callState.inCall,
      callPeerIds: [...this.callState.peerIds],
      pendingDrafts: { ...this.pendingDrafts },
      pendingTransfers: { ...this.pendingTransfers },
      consecutiveCrashes: this.getConsecutiveCrashes(),
      lastError: this.lastError,
      uptimeMs: Date.now() - this.startTime,
    };

    storageSet(CRASH_JOURNAL_KEY, JSON.stringify(record));
  }

  private readCrashJournal(): CrashRecord | null {
    try {
      const raw = storageGet(CRASH_JOURNAL_KEY);
      if (!raw) return null;
      return JSON.parse(raw) as CrashRecord;
    } catch {
      return null;
    }
  }

  // ── Consecutive Crash Tracking ────────────────────────────

  private updateConsecutiveCrashCount(tier: CrashTier): void {
    if (tier === 'clean') return;
    const current = this.getConsecutiveCrashes();
    storageSet(CONSECUTIVE_CRASH_KEY, String(current + 1));
  }

  private resetConsecutiveCrashCount(): void {
    storageSet(CONSECUTIVE_CRASH_KEY, '0');
  }

  // ── Recovery Execution ────────────────────────────────────

  /**
   * Execute a recovery plan. Called by the startup orchestrator.
   * Each action is executed in order; failures are logged but don't block.
   */
  async executeRecoveryPlan(
    plan: RecoveryPlan,
    executors: Partial<Record<RecoveryAction, () => Promise<boolean>>>,
  ): Promise<RecoveryResult> {
    const startTime = Date.now();
    const executed: RecoveryAction[] = [];
    const failed: RecoveryAction[] = [];

    for (const action of plan.actions) {
      const executor = executors[action];
      if (!executor) {
        // No executor registered — skip
        continue;
      }

      try {
        const success = await executor();
        if (success) {
          executed.push(action);
        } else {
          failed.push(action);
        }
      } catch (err) {
        console.error(`[CrashRecovery] Action "${action}" failed:`, (err as Error).message);
        failed.push(action);
      }
    }

    // Clear crash journal after successful recovery
    if (failed.length === 0) {
      storageRemove(CRASH_JOURNAL_KEY);
    }

    return {
      success: failed.length === 0,
      tier: plan.tier,
      actionsExecuted: executed,
      actionsFailed: failed,
      timingMs: Date.now() - startTime,
      userNotified: plan.showRecoveryUI,
    };
  }
}

// ── Singleton Export ────────────────────────────────────────

export const crashRecoveryManager = new CrashRecoveryManager();
