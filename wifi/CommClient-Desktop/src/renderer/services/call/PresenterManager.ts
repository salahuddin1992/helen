/**
 * PresenterManager — group screen share presenter management.
 *
 * In a group call, only one person can present their screen at a time
 * (exclusive presenter mode). This manager handles:
 *
 *   - Requesting presenter role (with server-side lock)
 *   - Releasing presenter role
 *   - Presenter switching (current presenter stops, new one starts)
 *   - Presenter queue (FIFO — next in line auto-starts when current stops)
 *   - Tracking who is currently presenting
 *   - Remote screen stream routing (which participant's screen to display)
 *   - Presenter permissions (admin can force-stop)
 *
 * The server is the source of truth for who holds the presenter lock.
 * This client-side manager syncs with the server via socket events.
 */

import { socketManager } from '../socket.manager';

// ── Types ───────────────────────────────────────────

export interface PresenterInfo {
  userId: string;
  displayName: string;
  startedAt: number;
}

export type PresenterRequestStatus =
  | 'idle'
  | 'requesting'
  | 'granted'
  | 'denied'
  | 'queued';

export interface PresenterState {
  /** Current active presenter (null if nobody is presenting) */
  currentPresenter: PresenterInfo | null;
  /** Users waiting in queue to present */
  queue: PresenterInfo[];
  /** Local user's request status */
  localRequestStatus: PresenterRequestStatus;
  /** Position in queue (0 = not queued, 1+ = position) */
  queuePosition: number;
  /** Whether local user is the current presenter */
  isLocalPresenter: boolean;
  /** Remote presenter's screen stream (for viewers) */
  remoteScreenStream: MediaStream | null;
  /** Presenter's userId whose screen we're viewing */
  viewingUserId: string | null;
}

export interface PresenterCallbacks {
  onStateChange: (state: PresenterState) => void;
  /** Presenter role granted to local user — start screen share now */
  onPresenterGranted: () => void;
  /** Presenter role revoked from local user (admin forced, or replaced) */
  onPresenterRevoked: (reason: string) => void;
  /** New remote presenter started — display their screen */
  onRemotePresenterStarted: (presenter: PresenterInfo) => void;
  /** Remote presenter stopped */
  onRemotePresenterStopped: () => void;
  /** Queue position changed */
  onQueueUpdate: (position: number) => void;
}

// ── Manager ─────────────────────────────────────────

export class PresenterManager {
  private callbacks: PresenterCallbacks;
  private localUserId: string;
  private callId: string | null = null;
  private _state: PresenterState;
  private _socketUnsubs: Array<() => void> = [];
  private _destroyed = false;

  // Presenter timeout
  private _presenterTimeoutTimer: NodeJS.Timeout | null = null;
  private _presenterTimeoutMs: number = 0;

  // Request timeout and cooldown
  private _requestTimeoutMs: number = 10000; // 10 seconds
  private _lastRequestTime: number = 0;
  private _requestCooldownMs: number = 2000; // 2 second cooldown

  // Viewer count tracking
  private _viewerCount: number = 0;

  // Activity tracking
  private _lastActivity: number = Date.now();

  constructor(localUserId: string, callbacks: PresenterCallbacks) {
    this.localUserId = localUserId;
    this.callbacks = callbacks;

    this._state = {
      currentPresenter: null,
      queue: [],
      localRequestStatus: 'idle',
      queuePosition: 0,
      isLocalPresenter: false,
      remoteScreenStream: null,
      viewingUserId: null,
    };
  }

  get state(): PresenterState {
    return { ...this._state };
  }

  /**
   * Initialize — register socket listeners.
   */
  init(callId: string): void {
    this.callId = callId;
    this._registerSocketListeners();
  }

  // ── Request / Release ─────────────────────────────

  /**
   * Request the presenter role.
   * Server will grant immediately if nobody is presenting,
   * or queue the request if someone else is.
   */
  async requestPresenter(): Promise<PresenterRequestStatus> {
    if (this._state.isLocalPresenter) return 'granted';
    if (this._state.localRequestStatus === 'requesting') return 'requesting';

    // Suppress rapid-fire requests (cooldown)
    const now = Date.now();
    if (now - this._lastRequestTime < this._requestCooldownMs) {
      return this._state.localRequestStatus;
    }
    this._lastRequestTime = now;

    this._updateState({ localRequestStatus: 'requesting' });

    try {
      // Request with timeout
      const response = await Promise.race([
        socketManager.emit('presenter_request', {
          call_id: this.callId,
        }),
        new Promise((_, reject) =>
          setTimeout(
            () => reject(new Error('Request timeout')),
            this._requestTimeoutMs
          )
        ),
      ]);

      if (response?.error) {
        this._updateState({ localRequestStatus: 'denied' });
        return 'denied';
      }

      const status = response?.status as PresenterRequestStatus;

      if (status === 'granted') {
        this._updateState({
          localRequestStatus: 'granted',
          isLocalPresenter: true,
          currentPresenter: {
            userId: this.localUserId,
            displayName: 'You',
            startedAt: Date.now(),
          },
        });
        this.callbacks.onPresenterGranted();
      } else if (status === 'queued') {
        this._updateState({
          localRequestStatus: 'queued',
          queuePosition: response?.position || 0,
        });
      }

      return status;
    } catch (e: any) {
      console.error('[PresenterManager] Request failed:', e);
      this._updateState({ localRequestStatus: 'idle' });
      return 'denied';
    }
  }

  /**
   * Release the presenter role.
   */
  async releasePresenter(): Promise<void> {
    if (!this._state.isLocalPresenter && this._state.localRequestStatus !== 'queued') return;

    socketManager.emitNoAck('presenter_release', {
      call_id: this.callId,
    });

    this._updateState({
      localRequestStatus: 'idle',
      isLocalPresenter: false,
      queuePosition: 0,
    });

    // If we were the current presenter, clear it
    if (this._state.currentPresenter?.userId === this.localUserId) {
      this._updateState({ currentPresenter: null });
    }
  }

  /**
   * Cancel a queued presenter request.
   */
  cancelRequest(): void {
    if (this._state.localRequestStatus !== 'queued') return;

    socketManager.emitNoAck('presenter_cancel_request', {
      call_id: this.callId,
    });

    this._updateState({
      localRequestStatus: 'idle',
      queuePosition: 0,
    });
  }

  /**
   * Force-stop current presenter (admin action).
   */
  forceStopPresenter(targetUserId: string): void {
    socketManager.emitNoAck('presenter_force_stop', {
      call_id: this.callId,
      target_user_id: targetUserId,
    });
  }

  /**
   * Enable presenter auto-release after inactivity.
   */
  enablePresenterTimeout(timeoutMs?: number): void {
    if (timeoutMs) {
      this._presenterTimeoutMs = timeoutMs;
    } else {
      this._presenterTimeoutMs = 5 * 60 * 1000; // 5 minute default
    }
  }

  /**
   * Disable auto-release timeout.
   */
  disablePresenterTimeout(): void {
    if (this._presenterTimeoutTimer) {
      clearTimeout(this._presenterTimeoutTimer);
      this._presenterTimeoutTimer = null;
    }
    this._presenterTimeoutMs = 0;
  }

  /**
   * Handoff presenter role to another user.
   * Current presenter releases and requests server to transfer to target.
   */
  async handoffTo(userId: string): Promise<boolean> {
    if (!this._state.isLocalPresenter) {
      console.warn('[PresenterManager] Only current presenter can handoff');
      return false;
    }

    try {
      const response = await Promise.race([
        socketManager.emit('presenter_handoff', {
          call_id: this.callId,
          target_user_id: userId,
        }),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('Handoff timeout')), 5000)
        ),
      ]);

      if (response?.success) {
        return true;
      }
      return false;
    } catch (e: any) {
      console.error('[PresenterManager] Handoff failed:', e);
      return false;
    }
  }

  /**
   * Get local user's position in the presenter queue.
   */
  getQueuePosition(): number {
    const position = this._state.queue.findIndex(
      (q) => q.userId === this.localUserId
    );
    return position >= 0 ? position + 1 : -1;
  }

  /**
   * Estimate wait time before becoming presenter (30s per queue position).
   */
  getEstimatedWaitTime(): number {
    const position = this.getQueuePosition();
    if (position <= 0) return 0;
    return position * 30000; // 30 seconds per position
  }

  /**
   * Check if queue is at max capacity.
   */
  isQueueFull(): boolean {
    const maxQueueSize = 10;
    return this._state.queue.length >= maxQueueSize;
  }

  /**
   * Get current viewer count for the presenter's stream.
   */
  getViewerCount(): number {
    return this._viewerCount;
  }

  /**
   * Record activity (called by presenter when doing something).
   */
  reportActivity(): void {
    this._lastActivity = Date.now();
    if (this._presenterTimeoutMs > 0 && this._state.isLocalPresenter) {
      this._resetPresenterTimeout();
    }
  }

  /**
   * Get idle duration since last activity (ms).
   */
  getIdleDuration(): number {
    return Date.now() - this._lastActivity;
  }

  // ── Remote Screen Stream ──────────────────────────

  /**
   * Set the remote screen stream received via WebRTC.
   * Called when we detect a new screen track from the presenter's peer connection.
   */
  setRemoteScreenStream(userId: string, stream: MediaStream): void {
    this._updateState({
      remoteScreenStream: stream,
      viewingUserId: userId,
    });
  }

  /**
   * Clear the remote screen stream.
   */
  clearRemoteScreenStream(): void {
    this._updateState({
      remoteScreenStream: null,
      viewingUserId: null,
    });
  }

  // ── Socket Listeners ──────────────────────────────

  private _registerSocketListeners(): void {
    // Presenter granted (could be us or someone else)
    this._socketUnsubs.push(
      socketManager.on('presenter_granted', (data: any) => {
        if (data.call_id !== this.callId) return;

        const presenter: PresenterInfo = {
          userId: data.user_id,
          displayName: data.display_name || data.user_id,
          startedAt: Date.now(),
        };

        const isLocal = data.user_id === this.localUserId;

        this._updateState({
          currentPresenter: presenter,
          isLocalPresenter: isLocal,
          localRequestStatus: isLocal ? 'granted' : this._state.localRequestStatus,
        });

        if (isLocal) {
          // Start presenter timeout if enabled
          if (this._presenterTimeoutMs > 0) {
            this._resetPresenterTimeout();
          }
          this.callbacks.onPresenterGranted();
        } else {
          this.callbacks.onRemotePresenterStarted(presenter);
        }
      })
    );

    // Presenter released
    this._socketUnsubs.push(
      socketManager.on('presenter_released', (data: any) => {
        if (data.call_id !== this.callId) return;

        const wasLocal = this._state.currentPresenter?.userId === this.localUserId;

        this._updateState({
          currentPresenter: null,
          remoteScreenStream: null,
          viewingUserId: null,
        });

        if (wasLocal) {
          this._updateState({
            isLocalPresenter: false,
            localRequestStatus: 'idle',
          });
        }

        this.callbacks.onRemotePresenterStopped();
      })
    );

    // Presenter force-stopped (by admin)
    this._socketUnsubs.push(
      socketManager.on('presenter_force_stopped', (data: any) => {
        if (data.call_id !== this.callId) return;

        const wasLocal = data.user_id === this.localUserId;

        this._updateState({
          currentPresenter: null,
          remoteScreenStream: null,
          viewingUserId: null,
        });

        if (wasLocal) {
          this._updateState({
            isLocalPresenter: false,
            localRequestStatus: 'idle',
          });
          this.callbacks.onPresenterRevoked(data.reason || 'Stopped by admin');
        }

        this.callbacks.onRemotePresenterStopped();
      })
    );

    // Queue updated
    this._socketUnsubs.push(
      socketManager.on('presenter_queue_update', (data: any) => {
        if (data.call_id !== this.callId) return;

        const queue: PresenterInfo[] = (data.queue || []).map((q: any) => ({
          userId: q.user_id,
          displayName: q.display_name || q.user_id,
          startedAt: 0,
        }));

        const myPosition = queue.findIndex((q) => q.userId === this.localUserId) + 1;

        this._updateState({
          queue,
          queuePosition: myPosition,
        });

        this.callbacks.onQueueUpdate(myPosition);
      })
    );

    // Presenter auto-promoted from queue
    this._socketUnsubs.push(
      socketManager.on('presenter_promoted', (data: any) => {
        if (data.call_id !== this.callId) return;
        if (data.user_id !== this.localUserId) return;

        this._updateState({
          localRequestStatus: 'granted',
          isLocalPresenter: true,
          queuePosition: 0,
          currentPresenter: {
            userId: this.localUserId,
            displayName: 'You',
            startedAt: Date.now(),
          },
        });

        // Start presenter timeout if enabled
        if (this._presenterTimeoutMs > 0) {
          this._resetPresenterTimeout();
        }

        this.callbacks.onPresenterGranted();
      })
    );

    // Presenter handoff accepted
    this._socketUnsubs.push(
      socketManager.on('presenter_handoff_accepted', (data: any) => {
        if (data.call_id !== this.callId) return;
        console.log('[PresenterManager] Handoff accepted by', data.target_user_id);
      })
    );

    // Viewer count updates
    this._socketUnsubs.push(
      socketManager.on('presenter_viewer_count', (data: any) => {
        if (data.call_id !== this.callId) return;
        this._viewerCount = data.viewer_count || 0;
      })
    );
  }

  // ── Private ───────────────────────────────────────

  private _updateState(partial: Partial<PresenterState>): void {
    this._state = { ...this._state, ...partial };
    this.callbacks.onStateChange({ ...this._state });
  }

  private _resetPresenterTimeout(): void {
    // Clear existing timeout
    if (this._presenterTimeoutTimer) {
      clearTimeout(this._presenterTimeoutTimer);
    }

    // Only set timeout if we're the presenter and timeout is enabled
    if (!this._state.isLocalPresenter || this._presenterTimeoutMs <= 0) {
      return;
    }

    this._presenterTimeoutTimer = setTimeout(() => {
      const idleDuration = this.getIdleDuration();
      if (idleDuration >= this._presenterTimeoutMs) {
        console.log(
          '[PresenterManager] Auto-releasing presenter due to inactivity'
        );
        this.releasePresenter();
      }
    }, this._presenterTimeoutMs);
  }

  // ── Cleanup ───────────────────────────────────────

  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;

    // Stop any active timeouts
    if (this._presenterTimeoutTimer) {
      clearTimeout(this._presenterTimeoutTimer);
      this._presenterTimeoutTimer = null;
    }

    // Release presenter if we hold it
    if (this._state.isLocalPresenter) {
      this.releasePresenter();
    }

    for (const unsub of this._socketUnsubs) {
      unsub();
    }
    this._socketUnsubs = [];
  }
}
