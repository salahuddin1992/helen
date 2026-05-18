/**
 * CallStateMachine — formal finite state machine for call lifecycle.
 *
 * States:
 *   idle → ringing → connecting → active → reconnecting → active
 *                                       → ended
 *   idle → ringing → ended (rejected/missed)
 *
 * Every transition is validated. Invalid transitions are logged and rejected.
 */

export type CallStatus =
  | 'idle'
  | 'ringing'
  | 'connecting'
  | 'active'
  | 'reconnecting'
  | 'ended';

export type CallEvent =
  | 'INITIATE'
  | 'INCOMING'
  | 'ACCEPT'
  | 'REJECT'
  | 'PEER_READY'
  | 'CONNECTED'
  | 'DISCONNECTED'
  | 'RECONNECTED'
  | 'RECONNECT_FAILED'
  | 'HANGUP'
  | 'REMOTE_HANGUP'
  | 'ERROR'
  | 'TIMEOUT';

type TransitionMap = Record<CallStatus, Partial<Record<CallEvent, CallStatus>>>;

const TRANSITIONS: TransitionMap = {
  idle: {
    INITIATE: 'ringing',
    INCOMING: 'ringing',
  },
  ringing: {
    ACCEPT: 'connecting',
    PEER_READY: 'connecting',
    REJECT: 'ended',
    HANGUP: 'ended',
    REMOTE_HANGUP: 'ended',
    TIMEOUT: 'ended',
    ERROR: 'ended',
  },
  connecting: {
    CONNECTED: 'active',
    HANGUP: 'ended',
    REMOTE_HANGUP: 'ended',
    TIMEOUT: 'ended',
    ERROR: 'ended',
    DISCONNECTED: 'reconnecting',
  },
  active: {
    DISCONNECTED: 'reconnecting',
    HANGUP: 'ended',
    REMOTE_HANGUP: 'ended',
    ERROR: 'ended',
  },
  reconnecting: {
    RECONNECTED: 'active',
    RECONNECT_FAILED: 'ended',
    HANGUP: 'ended',
    REMOTE_HANGUP: 'ended',
    TIMEOUT: 'ended',
    ERROR: 'ended',
  },
  ended: {
    // Terminal. Only INITIATE or INCOMING from idle restarts.
  },
};

export type StateChangeCallback = (
  prev: CallStatus,
  next: CallStatus,
  event: CallEvent
) => void;

export class CallStateMachine {
  private _state: CallStatus = 'idle';
  private _listeners: StateChangeCallback[] = [];
  private _history: Array<{ from: CallStatus; to: CallStatus; event: CallEvent; ts: number }> = [];

  get state(): CallStatus {
    return this._state;
  }

  get history() {
    return [...this._history];
  }

  /**
   * Attempt a state transition. Returns true if the transition was valid.
   */
  transition(event: CallEvent): boolean {
    const next = TRANSITIONS[this._state]?.[event];
    if (!next) {
      console.warn(
        `[CallFSM] Invalid transition: ${this._state} + ${event} → (blocked)`
      );
      return false;
    }

    const prev = this._state;
    this._state = next;
    this._history.push({ from: prev, to: next, event, ts: Date.now() });

    // Keep history bounded
    if (this._history.length > 50) this._history.shift();

    console.log(`[CallFSM] ${prev} → ${next}  (event: ${event})`);
    for (const cb of this._listeners) {
      try {
        cb(prev, next, event);
      } catch (e) {
        console.error('[CallFSM] listener error:', e);
      }
    }
    return true;
  }

  /**
   * Force state to idle (used on cleanup).
   */
  reset(): void {
    this._state = 'idle';
    this._history = [];
  }

  /**
   * Check if a transition is valid without performing it.
   */
  canTransition(event: CallEvent): boolean {
    return !!TRANSITIONS[this._state]?.[event];
  }

  /**
   * Is the call in a "live" state (not idle or ended)?
   */
  get isLive(): boolean {
    return !['idle', 'ended'].includes(this._state);
  }

  /**
   * Subscribe to state changes. Returns unsubscribe function.
   */
  onChange(cb: StateChangeCallback): () => void {
    this._listeners.push(cb);
    return () => {
      this._listeners = this._listeners.filter((l) => l !== cb);
    };
  }

  /**
   * Remove all listeners.
   */
  removeAllListeners(): void {
    this._listeners = [];
  }
}
