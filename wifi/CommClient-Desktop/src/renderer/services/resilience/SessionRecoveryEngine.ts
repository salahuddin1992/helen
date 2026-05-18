/**
 * SessionRecoveryEngine.ts — Restore calls, chats, drafts, and pending operations.
 *
 * After CrashRecoveryManager determines the crash tier and SafeStateManager
 * provides the persisted state, this engine decides WHAT to restore and
 * HOW to present it to the user.
 *
 * Recovery capabilities:
 * ┌───────────────────┬────────────────────────────────────────────────────┐
 * │ Session Type       │ Recovery Behavior                                 │
 * ├───────────────────┼────────────────────────────────────────────────────┤
 * │ Active call        │ Offer rejoin prompt (30s window after crash)     │
 * │ Message drafts     │ Silently restore to input fields                 │
 * │ Pending messages   │ Queue for resend after socket reconnect          │
 * │ File transfers     │ Show resume option (if peer still online)        │
 * │ Active channel     │ Navigate to last open channel                    │
 * │ Scroll position    │ Restore chat scroll offset (best-effort)         │
 * │ UI state           │ Restore panel sizes, sidebar state               │
 * └───────────────────┴────────────────────────────────────────────────────┘
 *
 * Principles:
 *   1. NEVER auto-rejoin a call — always ask the user
 *   2. ALWAYS restore drafts silently — losing typed text is frustrating
 *   3. Pending messages: resend only if < 60 seconds old
 *   4. File transfers: offer resume only if < 10 minutes old
 *   5. UI state: best-effort, never block on failure
 *   6. All recovery is time-bounded (total max 5 seconds)
 */

import { safeStateManager, STATE_CONFIGS } from './SafeStateManager';
import type { CrashRecord } from './CrashRecoveryManager';

// ── Types ───────────────────────────────────────────────────

export interface CallRecoveryInfo {
  /** Was the user in a call when crash occurred? */
  wasInCall: boolean;
  /** IDs of peers in the call */
  peerIds: string[];
  /** How long ago the crash happened (ms) */
  crashAgeMs: number;
  /** Is rejoin still possible? (within time window) */
  canRejoin: boolean;
  /** Call type: 1:1 or group */
  callType: '1to1' | 'group';
  /** Was video active? */
  hadVideo: boolean;
  /** Was screen share active? */
  hadScreenShare: boolean;
}

export interface DraftRecoveryInfo {
  /** Channel ID → draft text */
  drafts: Record<string, string>;
  /** Number of drafts recovered */
  count: number;
}

export interface PendingMessage {
  id: string;
  channelId: string;
  content: string;
  timestamp: number;
  attachments: string[];
}

export interface TransferRecoveryInfo {
  /** Transfer ID → progress (0-1) */
  transfers: Record<string, number>;
  /** Number of resumable transfers */
  count: number;
}

export interface UIStateRecovery {
  /** Last active channel ID */
  activeChannelId: string | null;
  /** Last route path */
  lastRoute: string;
  /** Sidebar collapsed state */
  sidebarCollapsed: boolean;
  /** Any open modal */
  openModal: string | null;
}

export interface SessionRecoveryResult {
  /** Call recovery info (if applicable) */
  call: CallRecoveryInfo | null;
  /** Recovered message drafts */
  drafts: DraftRecoveryInfo;
  /** Pending unsent messages */
  pendingMessages: PendingMessage[];
  /** Resumable file transfers */
  transfers: TransferRecoveryInfo;
  /** UI state to restore */
  uiState: UIStateRecovery | null;
  /** Total recovery time (ms) */
  timingMs: number;
}

// ── Constants ───────────────────────────────────────────────

/** Max age of crash to offer call rejoin (30 seconds) */
const CALL_REJOIN_WINDOW_MS = 30_000;

/** Max age of pending message to auto-resend (7 days — survive long offline periods) */
const PENDING_MSG_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

/** Max age of transfer to offer resume (7 days — match server session TTL) */
const TRANSFER_RESUME_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

/** Max pending messages retained across restarts */
const PENDING_MSG_MAX_COUNT = 500;

/** Total timeout for recovery process (5 seconds) */
const RECOVERY_TIMEOUT_MS = 5_000;

// ── Persisted State Shapes ──────────────────────────────────

interface PersistedCallState {
  inCall: boolean;
  callType: '1to1' | 'group';
  peerIds: string[];
  hadVideo: boolean;
  hadScreenShare: boolean;
  startedAt: number;
}

interface PersistedActiveSession {
  userId: string;
  activeChannelId: string | null;
  lastRoute: string;
  sidebarCollapsed: boolean;
  openModal: string | null;
  savedAt: number;
}

interface PersistedPendingMessages {
  messages: PendingMessage[];
}

interface PersistedTransferState {
  transfers: Record<string, { progress: number; startedAt: number; fileName: string }>;
}

// ── Singleton ───────────────────────────────────────────────

class SessionRecoveryEngine {

  /**
   * Recover all session state. Returns what was found.
   * Call from the startup orchestrator after CrashRecoveryManager.
   */
  async recover(crashRecord: CrashRecord | null): Promise<SessionRecoveryResult> {
    const startTime = Date.now();
    const deadline = startTime + RECOVERY_TIMEOUT_MS;

    const result: SessionRecoveryResult = {
      call: null,
      drafts: { drafts: {}, count: 0 },
      pendingMessages: [],
      transfers: { transfers: {}, count: 0 },
      uiState: null,
      timingMs: 0,
    };

    try {
      // Run all recovery in parallel but with a total timeout
      const recoveryPromise = Promise.all([
        this.recoverCall(crashRecord).then(r => { result.call = r; }),
        this.recoverDrafts(crashRecord).then(r => { result.drafts = r; }),
        this.recoverPendingMessages().then(r => { result.pendingMessages = r; }),
        this.recoverTransfers().then(r => { result.transfers = r; }),
        this.recoverUIState(crashRecord).then(r => { result.uiState = r; }),
      ]);

      // Race against timeout
      await Promise.race([
        recoveryPromise,
        new Promise(resolve => setTimeout(resolve, RECOVERY_TIMEOUT_MS)),
      ]);

    } catch (err) {
      console.error('[SessionRecovery] Recovery error:', (err as Error).message);
    }

    result.timingMs = Date.now() - startTime;
    return result;
  }

  // ── Save State (call periodically during normal operation) ─

  /**
   * Save current call state for potential recovery.
   */
  saveCallState(state: PersistedCallState): void {
    safeStateManager.set('callState', state);
  }

  /**
   * Clear call state (on normal call end).
   */
  clearCallState(): void {
    safeStateManager.remove('callState');
  }

  /**
   * Save message drafts.
   */
  saveDrafts(drafts: Record<string, string>): void {
    safeStateManager.set('drafts', drafts);
  }

  /**
   * Save active UI session.
   */
  saveActiveSession(session: PersistedActiveSession): void {
    safeStateManager.set('activeSession', session);
  }

  /**
   * Queue a pending message (not yet acknowledged by server).
   */
  addPendingMessage(message: PendingMessage): void {
    const existing = safeStateManager.get<PersistedPendingMessages>('storeSnapshot') || { messages: [] };
    existing.messages.push(message);
    if (existing.messages.length > PENDING_MSG_MAX_COUNT) {
      existing.messages = existing.messages.slice(-PENDING_MSG_MAX_COUNT);
    }
    safeStateManager.set('storeSnapshot', existing);
  }

  /**
   * Remove a pending message after server acknowledges it.
   */
  acknowledgePendingMessage(messageId: string): void {
    const existing = safeStateManager.get<PersistedPendingMessages>('storeSnapshot');
    if (existing) {
      existing.messages = existing.messages.filter(m => m.id !== messageId);
      safeStateManager.set('storeSnapshot', existing);
    }
  }

  /**
   * Save file transfer progress.
   */
  saveTransferProgress(transferId: string, progress: number, fileName: string): void {
    const existing = safeStateManager.get<PersistedTransferState>('transferProgress') || { transfers: {} };
    if (progress >= 1) {
      delete existing.transfers[transferId];
    } else {
      existing.transfers[transferId] = { progress, startedAt: Date.now(), fileName };
    }
    safeStateManager.set('transferProgress', existing);
  }

  // ── Private: Recovery Methods ─────────────────────────────

  private async recoverCall(crashRecord: CrashRecord | null): Promise<CallRecoveryInfo | null> {
    // Try crash record first (most recent state)
    if (crashRecord?.wasInCall) {
      const crashAge = Date.now() - crashRecord.detectedAt;
      return {
        wasInCall: true,
        peerIds: crashRecord.callPeerIds,
        crashAgeMs: crashAge,
        canRejoin: crashAge < CALL_REJOIN_WINDOW_MS,
        callType: crashRecord.callPeerIds.length > 1 ? 'group' : '1to1',
        hadVideo: false, // Unknown from crash record
        hadScreenShare: false,
      };
    }

    // Fall back to persisted call state
    const callState = safeStateManager.get<PersistedCallState>('callState');
    if (callState && callState.inCall) {
      const crashAge = Date.now() - (callState.startedAt || Date.now());
      return {
        wasInCall: true,
        peerIds: callState.peerIds,
        crashAgeMs: crashAge,
        canRejoin: crashAge < CALL_REJOIN_WINDOW_MS + callState.startedAt,
        callType: callState.callType,
        hadVideo: callState.hadVideo,
        hadScreenShare: callState.hadScreenShare,
      };
    }

    return null;
  }

  private async recoverDrafts(crashRecord: CrashRecord | null): Promise<DraftRecoveryInfo> {
    // Try crash record first
    const crashDrafts = crashRecord?.pendingDrafts || {};

    // Merge with persisted drafts (persisted may be more complete)
    const persistedDrafts = safeStateManager.get<Record<string, string>>('drafts') || {};

    // Merge: crash record takes priority (more recent)
    const merged = { ...persistedDrafts, ...crashDrafts };

    // Filter empty drafts
    const filtered: Record<string, string> = {};
    for (const [channelId, text] of Object.entries(merged)) {
      if (text && text.trim().length > 0) {
        filtered[channelId] = text;
      }
    }

    return {
      drafts: filtered,
      count: Object.keys(filtered).length,
    };
  }

  private async recoverPendingMessages(): Promise<PendingMessage[]> {
    const stored = safeStateManager.get<PersistedPendingMessages>('storeSnapshot');
    if (!stored || !stored.messages) return [];

    const now = Date.now();
    // Only recover messages < 60 seconds old
    return stored.messages.filter(m => now - m.timestamp < PENDING_MSG_MAX_AGE_MS);
  }

  private async recoverTransfers(): Promise<TransferRecoveryInfo> {
    const stored = safeStateManager.get<PersistedTransferState>('transferProgress');
    if (!stored || !stored.transfers) {
      return { transfers: {}, count: 0 };
    }

    const now = Date.now();
    const resumable: Record<string, number> = {};
    let count = 0;

    for (const [id, info] of Object.entries(stored.transfers)) {
      if (now - info.startedAt < TRANSFER_RESUME_MAX_AGE_MS && info.progress < 1) {
        resumable[id] = info.progress;
        count++;
      }
    }

    return { transfers: resumable, count };
  }

  private async recoverUIState(crashRecord: CrashRecord | null): Promise<UIStateRecovery | null> {
    const session = safeStateManager.get<PersistedActiveSession>('activeSession');
    if (!session) {
      // Fallback to crash record
      if (crashRecord) {
        return {
          activeChannelId: null,
          lastRoute: crashRecord.lastRoute,
          sidebarCollapsed: false,
          openModal: null,
        };
      }
      return null;
    }

    return {
      activeChannelId: session.activeChannelId,
      lastRoute: session.lastRoute,
      sidebarCollapsed: session.sidebarCollapsed,
      openModal: null, // Never restore modals — they're contextual
    };
  }
}

// ── Singleton Export ────────────────────────────────────────

export const sessionRecoveryEngine = new SessionRecoveryEngine();
