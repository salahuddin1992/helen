/**
 * ScreenShareEngine — integration layer that connects ScreenShareManager
 * and PresenterManager with the CallEngine and store.
 *
 * Provides a unified API for:
 *   - Starting/stopping screen share (1-to-1 or group)
 *   - Presenter role management (request/release/queue)
 *   - Source selection and switching
 *   - Quality control
 *   - Dual-track vs replace-camera mode
 *
 * Usage from store:
 *   screenShareEngine.startSharing(source, options)
 *   screenShareEngine.stopSharing()
 *   screenShareEngine.requestPresenter()
 *   screenShareEngine.releasePresenter()
 */

import { socketManager } from '../socket.manager';
import { PeerConnection } from './PeerConnection';
import { GroupCallManager } from './GroupCallManager';
import {
  ScreenShareManager,
  ScreenShareSource,
  ScreenShareMode,
  ScreenShareState,
  SCREEN_QUALITY_PRESETS,
} from './ScreenShareManager';
import {
  PresenterManager,
  PresenterState,
  PresenterInfo,
} from './PresenterManager';

// ── Types ───────────────────────────────────────────

export interface ScreenShareEngineState {
  /** Screen share status */
  shareState: ScreenShareState;
  /** Presenter state (group calls) */
  presenterState: PresenterState;
  /** Is this a group call */
  isGroupCall: boolean;
  /** Dual-stream mode active (camera + screen simultaneously) */
  isDualStream?: boolean;
  /** Screen share includes audio track */
  hasAudio?: boolean;
  /** Auto-release presenter lock enabled */
  autoReleaseEnabled?: boolean;
  /** Number of viewers watching this stream */
  viewerCount?: number;
}

export interface ScreenShareEngineCallbacks {
  onStateChange: (state: ScreenShareEngineState) => void;
  /** Screen share started successfully */
  onShareStarted: (stream: MediaStream) => void;
  /** Screen share stopped */
  onShareStopped: () => void;
  /** Error occurred */
  onError: (error: string) => void;
  /** Presenter role granted — auto-trigger screen share start */
  onPresenterGranted: () => void;
  /** Presenter role revoked — auto-stop screen share */
  onPresenterRevoked: (reason: string) => void;
  /** Remote presenter started — route their stream */
  onRemotePresenterStarted: (presenter: PresenterInfo) => void;
  /** Remote presenter stopped */
  onRemotePresenterStopped: () => void;
}

export interface ShareOptions {
  mode?: ScreenShareMode;
  preset?: string;
  localStream?: MediaStream | null;
  withAudio?: boolean;
}

export interface ScreenShareAnalytics {
  totalShareDuration: number;
  presenterRequestCount: number;
  presenterGrantLatencyMs: number;
  sourceSwitchCount: number;
  qualityChangeCount: number;
  viewerCount: number;
}

// ── Engine ──────────────────────────────────────────

export class ScreenShareEngine {
  private screenManager: ScreenShareManager;
  private presenterManager: PresenterManager | null = null;
  private callbacks: ScreenShareEngineCallbacks;

  private localUserId: string;
  private callId: string | null = null;
  private _isGroupCall = false;
  private _destroyed = false;

  // Pending source for auto-start after presenter grant
  private _pendingSource: ScreenShareSource | null = null;
  private _pendingOptions: ShareOptions | null = null;

  // Dual-stream and audio tracking
  private _isDualStream = false;
  private _hasAudio = false;
  private _viewerCount = 0;

  // Auto-release presenter
  private _autoReleaseTimer: NodeJS.Timeout | null = null;
  private _autoReleaseTimeout = 5 * 60 * 1000; // 5 minutes default
  private _lastActivityTime = Date.now();
  private _autoReleaseEnabled = false;

  // Analytics
  private _shareStartTime: number = 0;
  private _presenterRequestCount = 0;
  private _presenterRequestTime: number = 0;
  private _sourceSwitchCount = 0;
  private _qualityChangeCount = 0;

  constructor(localUserId: string, callbacks: ScreenShareEngineCallbacks) {
    this.localUserId = localUserId;
    this.callbacks = callbacks;

    this.screenManager = new ScreenShareManager({
      onStateChange: (shareState) => {
        this._emitState();
      },
      onSourceEnded: () => {
        // OS-level stop or window closed
        this._handleShareEnded();
      },
      onError: (error) => {
        this.callbacks.onError(error);
      },
    });
  }

  get isSharing(): boolean {
    return this.screenManager.isSharing;
  }

  get shareState(): ScreenShareState {
    return this.screenManager.state;
  }

  get presenterState(): PresenterState | null {
    return this.presenterManager?.state || null;
  }

  // ── Initialization ────────────────────────────────

  /**
   * Initialize for a 1-to-1 call.
   */
  initForP2P(callId: string, pc: PeerConnection): void {
    this.callId = callId;
    this._isGroupCall = false;
    this.screenManager.attachPeer(pc);
    // No presenter manager needed for 1-to-1
  }

  /**
   * Initialize for a group call.
   */
  initForGroup(callId: string, gm: GroupCallManager): void {
    this.callId = callId;
    this._isGroupCall = true;
    this.screenManager.attachGroup(gm);

    // Create presenter manager for group calls
    this.presenterManager = new PresenterManager(this.localUserId, {
      onStateChange: () => {
        this._emitState();
      },
      onPresenterGranted: () => {
        this.callbacks.onPresenterGranted();
        // Auto-start sharing if we have a pending source
        if (this._pendingSource) {
          this._startShareAfterGrant();
        }
      },
      onPresenterRevoked: (reason) => {
        // Auto-stop sharing
        this.screenManager.stop();
        this._notifyServerShareStop();
        this.callbacks.onPresenterRevoked(reason);
        this.callbacks.onShareStopped();
      },
      onRemotePresenterStarted: (presenter) => {
        this.callbacks.onRemotePresenterStarted(presenter);
      },
      onRemotePresenterStopped: () => {
        this.callbacks.onRemotePresenterStopped();
      },
      onQueueUpdate: (_position) => {
        this._emitState();
      },
    });

    this.presenterManager.init(callId);
  }

  // ── Start / Stop Sharing ──────────────────────────

  /**
   * Start screen sharing.
   *
   * For 1-to-1 calls: starts immediately.
   * For group calls: requests presenter role first, then starts on grant.
   */
  async startSharing(
    source: ScreenShareSource,
    options: ShareOptions = {},
  ): Promise<void> {
    if (this._destroyed) return;

    if (this._isGroupCall && this.presenterManager) {
      // Group call — need presenter lock first
      this._pendingSource = source;
      this._pendingOptions = options;

      const status = await this.presenterManager.requestPresenter();

      if (status === 'granted') {
        // Already granted (no current presenter) — start immediately
        await this._doStartShare(source, options);
      } else if (status === 'queued') {
        // Queued — will auto-start via onPresenterGranted callback
        console.log('[ScreenShareEngine] Queued for presenter role');
      } else {
        this._pendingSource = null;
        this._pendingOptions = null;
        this.callbacks.onError('Presenter request denied');
      }
    } else {
      // 1-to-1 — start directly
      await this._doStartShare(source, options);
    }
  }

  /**
   * Stop screen sharing.
   */
  async stopSharing(): Promise<void> {
    if (!this.screenManager.isSharing) return;

    await this.screenManager.stop();
    this._notifyServerShareStop();

    // Release presenter lock in group calls
    if (this._isGroupCall && this.presenterManager) {
      await this.presenterManager.releasePresenter();
    }

    this._pendingSource = null;
    this._pendingOptions = null;

    this.callbacks.onShareStopped();
  }

  // ── Source Switching ───────────────────────────────

  /**
   * Switch to a different screen/window source without stopping.
   */
  async switchSource(newSource: ScreenShareSource, preset?: string): Promise<void> {
    if (!this.screenManager.isSharing) return;
    this._sourceSwitchCount++;
    this._recordActivity();
    await this.screenManager.switchSource(newSource, preset);
  }

  // ── Pause / Resume ────────────────────────────────

  pause(): void {
    this.screenManager.pause();
  }

  resume(): void {
    this.screenManager.resume();
  }

  // ── Quality ───────────────────────────────────────

  async setQuality(preset: string): Promise<void> {
    this._qualityChangeCount++;
    this._recordActivity();
    await this.screenManager.setQuality(preset);
  }

  // ── Presenter Actions (group only) ────────────────

  async requestPresenter(): Promise<string> {
    if (!this.presenterManager) return 'denied';
    this._presenterRequestCount++;
    this._presenterRequestTime = Date.now();
    return this.presenterManager.requestPresenter();
  }

  async releasePresenter(): Promise<void> {
    if (!this.presenterManager) return;
    this._stopAutoRelease();
    return this.presenterManager.releasePresenter();
  }

  cancelPresenterRequest(): void {
    this.presenterManager?.cancelRequest();
    this._pendingSource = null;
    this._pendingOptions = null;
  }

  forceStopPresenter(targetUserId: string): void {
    this.presenterManager?.forceStopPresenter(targetUserId);
  }

  /**
   * Initiate a presenter handoff to another user.
   * Current presenter releases and server transfers lock to target.
   */
  async handoffPresenterTo(userId: string): Promise<void> {
    if (!this.presenterManager || !this.presenterManager.state.isLocalPresenter) {
      throw new Error('Only current presenter can handoff');
    }
    await this.presenterManager.handoffTo(userId);
  }

  /**
   * Request a specific quality preset from the current presenter.
   * Viewer-side: sends quality preference via socket.
   */
  requestPresenterQuality(preset: string): void {
    if (this._isGroupCall && this.presenterManager?.state.viewingUserId) {
      socketManager.emitNoAck('presenter_quality_request', {
        call_id: this.callId,
        preset,
        from_user_id: this.localUserId,
      });
    }
  }

  /**
   * Enable auto-release of presenter lock after inactivity.
   */
  enableAutoRelease(timeoutMs?: number): void {
    this._autoReleaseEnabled = true;
    if (timeoutMs) {
      this._autoReleaseTimeout = timeoutMs;
    }
    this._resetAutoReleaseTimer();
    this._emitState();
  }

  /**
   * Disable auto-release of presenter lock.
   */
  disableAutoRelease(): void {
    this._autoReleaseEnabled = false;
    this._stopAutoRelease();
    this._emitState();
  }

  /**
   * Start sharing screen simultaneously with camera (dual-stream mode).
   */
  async startDualStream(source: ScreenShareSource, preset?: string): Promise<void> {
    if (this._destroyed) return;

    const options: ShareOptions = {
      mode: 'dual-track',
      preset: preset || '1080p',
    };

    this._isDualStream = true;
    this._recordActivity();

    try {
      if (this._isGroupCall && this.presenterManager) {
        this._pendingSource = source;
        this._pendingOptions = options;
        const status = await this.presenterManager.requestPresenter();

        if (status === 'granted') {
          await this._doStartShare(source, options);
        } else if (status !== 'queued') {
          this._isDualStream = false;
          this._pendingSource = null;
          this._pendingOptions = null;
          this.callbacks.onError('Failed to start dual stream');
        }
      } else {
        await this._doStartShare(source, options);
      }
    } catch (e: any) {
      this._isDualStream = false;
      throw e;
    }
  }

  /**
   * Check if dual-stream mode is active.
   */
  isDualStreamActive(): boolean {
    return this._isDualStream && this.screenManager.isSharing;
  }

  /**
   * Start sharing screen with audio track included.
   */
  async startSharingWithAudio(
    source: ScreenShareSource,
    options?: ShareOptions,
  ): Promise<void> {
    if (this._destroyed) return;

    const opts: ShareOptions = {
      ...options,
      withAudio: true,
    };

    this._hasAudio = true;
    this._recordActivity();

    try {
      if (this._isGroupCall && this.presenterManager) {
        this._pendingSource = source;
        this._pendingOptions = opts;
        const status = await this.presenterManager.requestPresenter();

        if (status === 'granted') {
          await this._doStartShare(source, opts);
        } else if (status !== 'queued') {
          this._hasAudio = false;
          this._pendingSource = null;
          this._pendingOptions = null;
          this.callbacks.onError('Failed to start sharing with audio');
        }
      } else {
        await this._doStartShare(source, opts);
      }
    } catch (e: any) {
      this._hasAudio = false;
      throw e;
    }
  }

  /**
   * Get screen share analytics (duration, request counts, quality changes, etc).
   */
  getShareAnalytics(): ScreenShareAnalytics {
    const totalShareDuration = this._shareStartTime > 0
      ? Date.now() - this._shareStartTime
      : 0;

    const presenterGrantLatencyMs =
      this._presenterRequestTime > 0
        ? Math.max(0, Date.now() - this._presenterRequestTime)
        : 0;

    return {
      totalShareDuration,
      presenterRequestCount: this._presenterRequestCount,
      presenterGrantLatencyMs,
      sourceSwitchCount: this._sourceSwitchCount,
      qualityChangeCount: this._qualityChangeCount,
      viewerCount: this._viewerCount,
    };
  }

  /**
   * Set the remote screen stream (when viewing someone else's share).
   */
  setRemoteScreenStream(userId: string, stream: MediaStream): void {
    this.presenterManager?.setRemoteScreenStream(userId, stream);
  }

  clearRemoteScreenStream(): void {
    this.presenterManager?.clearRemoteScreenStream();
  }

  // ── Available Presets ─────────────────────────────

  getAvailablePresets(): Array<{ key: string; label: string }> {
    return Object.entries(SCREEN_QUALITY_PRESETS).map(([key, preset]) => ({
      key,
      label: preset.label,
    }));
  }

  // ── Private ───────────────────────────────────────

  private async _doStartShare(
    source: ScreenShareSource,
    options: ShareOptions,
  ): Promise<void> {
    try {
      this._shareStartTime = Date.now();
      this._recordActivity();

      const stream = await this.screenManager.start(
        source,
        options.mode || 'dual-track',
        options.preset || '1080p',
        options.localStream,
      );

      this._notifyServerShareStart();

      // Notify server if audio is included
      if (options.withAudio) {
        socketManager.emitNoAck('v2_call_screen_share_audio', {
          call_id: this.callId,
          with_audio: true,
        });
      }

      // Start auto-release if enabled
      if (this._autoReleaseEnabled) {
        this._resetAutoReleaseTimer();
      }

      this.callbacks.onShareStarted(stream);
    } catch (e: any) {
      // Release presenter on failure
      if (this._isGroupCall && this.presenterManager) {
        await this.presenterManager.releasePresenter();
      }
      throw e;
    }
  }

  private async _startShareAfterGrant(): Promise<void> {
    if (!this._pendingSource) return;

    const source = this._pendingSource;
    const options = this._pendingOptions || {};
    this._pendingSource = null;
    this._pendingOptions = null;

    try {
      await this._doStartShare(source, options);
    } catch (e: any) {
      console.error('[ScreenShareEngine] Auto-start after grant failed:', e);
    }
  }

  private _handleShareEnded(): void {
    // OS stopped the share (user clicked system stop button)
    this._notifyServerShareStop();

    if (this._isGroupCall && this.presenterManager) {
      this.presenterManager.releasePresenter();
    }

    this.callbacks.onShareStopped();
  }

  private _notifyServerShareStart(): void {
    socketManager.emitNoAck('v2_call_screen_share_start', {
      call_id: this.callId,
    });
  }

  private _notifyServerShareStop(): void {
    socketManager.emitNoAck('v2_call_screen_share_stop', {
      call_id: this.callId,
    });
  }

  private _emitState(): void {
    this.callbacks.onStateChange({
      shareState: this.screenManager.state,
      presenterState: this.presenterManager?.state || {
        currentPresenter: null,
        queue: [],
        localRequestStatus: 'idle',
        queuePosition: 0,
        isLocalPresenter: false,
        remoteScreenStream: null,
        viewingUserId: null,
      },
      isGroupCall: this._isGroupCall,
      isDualStream: this._isDualStream,
      hasAudio: this._hasAudio,
      autoReleaseEnabled: this._autoReleaseEnabled,
      viewerCount: this._viewerCount,
    });
  }

  private _recordActivity(): void {
    this._lastActivityTime = Date.now();
    if (this._autoReleaseEnabled) {
      this._resetAutoReleaseTimer();
    }
  }

  private _resetAutoReleaseTimer(): void {
    if (!this._autoReleaseEnabled || !this._isGroupCall) return;

    this._stopAutoRelease();

    this._autoReleaseTimer = setTimeout(() => {
      const idleDuration = Date.now() - this._lastActivityTime;
      if (idleDuration >= this._autoReleaseTimeout) {
        console.log(
          '[ScreenShareEngine] Auto-releasing presenter due to inactivity'
        );
        this.releasePresenter();
      }
    }, this._autoReleaseTimeout);
  }

  private _stopAutoRelease(): void {
    if (this._autoReleaseTimer) {
      clearTimeout(this._autoReleaseTimer);
      this._autoReleaseTimer = null;
    }
  }

  // ── Cleanup ───────────────────────────────────────

  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;

    this._stopAutoRelease();
    this.screenManager.destroy();
    this.presenterManager?.destroy();
    this._pendingSource = null;
    this._pendingOptions = null;
  }
}
