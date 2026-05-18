/**
 * SocketOptimizer.ts — Socket.IO traffic reduction & message batching.
 *
 * Identified problems:
 *   1. Heartbeat every 5s = 12 packets/min even when idle
 *   2. Presence updates sent individually (not batched)
 *   3. Typing indicators sent per-keystroke (should debounce)
 *   4. No message coalescing for rapid-fire sends
 *   5. Full objects transmitted when deltas would suffice
 *   6. Discovery polling at 3s interval is aggressive
 *
 * Optimizations:
 *   1. Adaptive heartbeat: 5s during calls → 15s during chat → 30s when idle
 *   2. Batch presence updates: coalesce multiple status changes
 *   3. Typing indicator debounce: emit at most once per 2 seconds
 *   4. Message coalescing: batch rapid sends into single socket emit
 *   5. Delta encoding: send only changed fields for status updates
 *   6. Socket event prioritization: calls > messages > presence > typing
 *
 * Architecture:
 *   Wraps socket.manager.ts emit path with optimization layer.
 *   Does NOT modify the socket manager — intercepts and batches.
 */

// ── Types ───────────────────────────────────────────────────

export type SocketPriority = 'critical' | 'high' | 'normal' | 'low';

export interface BatchConfig {
  /** Maximum time to hold messages before flushing (ms) */
  maxDelayMs: number;
  /** Maximum messages to accumulate before force-flush */
  maxBatchSize: number;
  /** Whether to merge identical events (keep latest) */
  deduplicateByEvent: boolean;
}

export interface SocketTrafficStats {
  /** Total events sent since start */
  totalSent: number;
  /** Total events saved by batching/dedup */
  savedByBatching: number;
  /** Total events saved by debouncing */
  savedByDebounce: number;
  /** Current heartbeat interval (ms) */
  currentHeartbeatMs: number;
  /** Bytes sent (estimated) */
  estimatedBytesSent: number;
  /** Bytes saved by delta encoding */
  bytesSavedByDelta: number;
  /** Events per minute (rolling average) */
  eventsPerMinute: number;
}

interface QueuedEvent {
  event: string;
  data: any;
  priority: SocketPriority;
  timestamp: number;
}

type EmitFunction = (event: string, data: any) => void;

// ── Constants ───────────────────────────────────────────────

/** Heartbeat intervals by app state */
const HEARTBEAT_INTERVALS = {
  inCall: 5_000,
  active: 15_000,
  idle: 30_000,
  hidden: 60_000,
} as const;

/** Typing indicator minimum interval */
const TYPING_DEBOUNCE_MS = 2_000;

/** Presence batch window */
const PRESENCE_BATCH_MS = 1_000;

/** Message batch window for rapid sends */
const MESSAGE_BATCH_MS = 100;

/** Events that should NEVER be batched or delayed */
const CRITICAL_EVENTS = new Set([
  'call:offer',
  'call:answer',
  'call:ice-candidate',
  'call:hangup',
  'call:reject',
  'call:accept',
  'webrtc:signal',
]);

/** Events that can be deduplicated (only latest matters) */
const DEDUP_EVENTS = new Set([
  'presence:update',
  'typing:start',
  'typing:stop',
  'user:status',
]);

// ── Priority Assignment ─────────────────────────────────────

function getEventPriority(event: string): SocketPriority {
  if (CRITICAL_EVENTS.has(event)) return 'critical';
  if (event.startsWith('call:') || event.startsWith('webrtc:')) return 'high';
  if (event.startsWith('message:') || event.startsWith('channel:')) return 'normal';
  return 'low';
}

// ── SocketOptimizer ─────────────────────────────────────────

export class SocketOptimizer {
  private _emit: EmitFunction;
  private _queue: QueuedEvent[] = [];
  private _flushTimer: ReturnType<typeof setTimeout> | null = null;
  private _destroyed = false;

  // Debounce state
  private _lastTypingEmit = new Map<string, number>();  // channelId → timestamp
  private _lastPresenceData: Record<string, any> | null = null;
  private _presenceBatchTimer: ReturnType<typeof setTimeout> | null = null;
  private _pendingPresence: Array<{ event: string; data: any }> = [];

  // Adaptive heartbeat
  private _heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private _currentHeartbeatMs: number = HEARTBEAT_INTERVALS.active;
  private _heartbeatEmit: (() => void) | null = null;

  // Stats
  private _stats: SocketTrafficStats = {
    totalSent: 0,
    savedByBatching: 0,
    savedByDebounce: 0,
    currentHeartbeatMs: HEARTBEAT_INTERVALS.active,
    estimatedBytesSent: 0,
    bytesSavedByDelta: 0,
    eventsPerMinute: 0,
  };
  private _eventTimestamps: number[] = [];

  constructor(emit: EmitFunction) {
    this._emit = emit;
  }

  // ── Lifecycle ─────────────────────────────────────────────

  destroy(): void {
    this._destroyed = true;
    this._flushNow();
    this._stopHeartbeat();
    if (this._presenceBatchTimer) {
      clearTimeout(this._presenceBatchTimer);
      this._presenceBatchTimer = null;
    }
    this._lastTypingEmit.clear();
    this._pendingPresence = [];
  }

  // ── Optimized Emit ────────────────────────────────────────

  /**
   * Send a socket event through the optimization layer.
   * Critical events bypass all batching. Others may be batched/debounced.
   */
  send(event: string, data: any): void {
    if (this._destroyed) return;

    const priority = getEventPriority(event);

    // Critical events: emit immediately, no batching
    if (priority === 'critical') {
      this._directEmit(event, data);
      return;
    }

    // Typing indicators: debounce
    if (event === 'typing:start' || event === 'typing:indicator') {
      if (this._debounceTyping(event, data)) return;
    }

    // Presence updates: batch
    if (event.startsWith('presence:') || event === 'user:status') {
      this._batchPresence(event, data);
      return;
    }

    // High priority: emit with minimal delay
    if (priority === 'high') {
      this._directEmit(event, data);
      return;
    }

    // Normal/low priority: queue for batching
    this._enqueue({ event, data, priority, timestamp: Date.now() });
  }

  // ── Adaptive Heartbeat ────────────────────────────────────

  /**
   * Start adaptive heartbeat. Adjusts interval based on app state.
   */
  startHeartbeat(emitHeartbeat: () => void): void {
    this._heartbeatEmit = emitHeartbeat;
    this._setHeartbeatInterval(HEARTBEAT_INTERVALS.active);
  }

  /**
   * Update heartbeat interval based on app state.
   */
  setAppState(state: 'inCall' | 'active' | 'idle' | 'hidden'): void {
    const interval = HEARTBEAT_INTERVALS[state];
    if (interval !== this._currentHeartbeatMs) {
      this._setHeartbeatInterval(interval);
    }
  }

  private _setHeartbeatInterval(ms: number): void {
    this._stopHeartbeat();
    this._currentHeartbeatMs = ms;
    this._stats.currentHeartbeatMs = ms;

    if (this._heartbeatEmit) {
      this._heartbeatTimer = setInterval(() => {
        if (!this._destroyed && this._heartbeatEmit) {
          this._heartbeatEmit();
        }
      }, ms);
    }
  }

  private _stopHeartbeat(): void {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
  }

  // ── Delta Encoding ────────────────────────────────────────

  /**
   * Compute delta between previous and current presence data.
   * Returns only changed fields, or null if nothing changed.
   */
  computePresenceDelta(current: Record<string, any>): Record<string, any> | null {
    if (!this._lastPresenceData) {
      this._lastPresenceData = { ...current };
      return current;
    }

    const delta: Record<string, any> = {};
    let hasChanges = false;

    for (const key of Object.keys(current)) {
      if (JSON.stringify(current[key]) !== JSON.stringify(this._lastPresenceData[key])) {
        delta[key] = current[key];
        hasChanges = true;
      }
    }

    if (!hasChanges) return null;

    // Update last known state
    this._lastPresenceData = { ...current };

    // Track bytes saved
    const fullSize = JSON.stringify(current).length;
    const deltaSize = JSON.stringify(delta).length;
    this._stats.bytesSavedByDelta += (fullSize - deltaSize);

    return delta;
  }

  // ── Stats ─────────────────────────────────────────────────

  getStats(): SocketTrafficStats {
    // Compute rolling events per minute
    const now = Date.now();
    const oneMinuteAgo = now - 60_000;
    this._eventTimestamps = this._eventTimestamps.filter(t => t > oneMinuteAgo);
    this._stats.eventsPerMinute = this._eventTimestamps.length;

    return { ...this._stats };
  }

  // ── Internal: Typing Debounce ─────────────────────────────

  private _debounceTyping(event: string, data: any): boolean {
    const channelId = data?.channelId || data?.channel_id || 'default';
    const lastEmit = this._lastTypingEmit.get(channelId) ?? 0;
    const now = Date.now();

    if (now - lastEmit < TYPING_DEBOUNCE_MS) {
      this._stats.savedByDebounce++;
      return true; // Skip this emit
    }

    this._lastTypingEmit.set(channelId, now);
    this._directEmit(event, data);
    return true; // We handled it
  }

  // ── Internal: Presence Batching ───────────────────────────

  private _batchPresence(event: string, data: any): void {
    this._pendingPresence.push({ event, data });

    if (!this._presenceBatchTimer) {
      this._presenceBatchTimer = setTimeout(() => {
        this._flushPresence();
      }, PRESENCE_BATCH_MS);
    }
  }

  private _flushPresence(): void {
    this._presenceBatchTimer = null;

    if (this._pendingPresence.length === 0) return;

    // Deduplicate: keep only the latest per event type
    const latest = new Map<string, any>();
    for (const { event, data } of this._pendingPresence) {
      latest.set(event, data);
    }

    const savedCount = this._pendingPresence.length - latest.size;
    this._stats.savedByBatching += savedCount;

    for (const [event, data] of latest) {
      this._directEmit(event, data);
    }

    this._pendingPresence = [];
  }

  // ── Internal: Event Queue ─────────────────────────────────

  private _enqueue(item: QueuedEvent): void {
    // Deduplicate: if same event already queued, replace with latest
    if (DEDUP_EVENTS.has(item.event)) {
      const existingIdx = this._queue.findIndex(q => q.event === item.event);
      if (existingIdx >= 0) {
        this._queue[existingIdx] = item;
        this._stats.savedByBatching++;
        return;
      }
    }

    this._queue.push(item);

    // Schedule flush
    if (!this._flushTimer) {
      this._flushTimer = setTimeout(() => {
        this._flushNow();
      }, MESSAGE_BATCH_MS);
    }

    // Force flush if queue too large
    if (this._queue.length >= 20) {
      this._flushNow();
    }
  }

  private _flushNow(): void {
    if (this._flushTimer) {
      clearTimeout(this._flushTimer);
      this._flushTimer = null;
    }

    // Sort by priority then timestamp
    const priorityOrder: Record<SocketPriority, number> = {
      critical: 0, high: 1, normal: 2, low: 3,
    };

    this._queue.sort((a, b) => {
      const pd = priorityOrder[a.priority] - priorityOrder[b.priority];
      return pd !== 0 ? pd : a.timestamp - b.timestamp;
    });

    for (const item of this._queue) {
      this._directEmit(item.event, item.data);
    }

    this._queue = [];
  }

  // ── Internal: Direct Emit ─────────────────────────────────

  private _directEmit(event: string, data: any): void {
    this._stats.totalSent++;
    this._eventTimestamps.push(Date.now());

    // Estimate bytes
    try {
      this._stats.estimatedBytesSent += event.length + JSON.stringify(data).length;
    } catch {}

    this._emit(event, data);
  }
}
