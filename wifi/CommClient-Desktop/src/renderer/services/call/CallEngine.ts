/**
 * CallEngine — top-level orchestrator for the calling subsystem.
 *
 * Unifies:
 *   - CallStateMachine — lifecycle FSM
 *   - MediaDeviceManager — device enumeration and local stream
 *   - PeerConnection — single-peer WebRTC wrapper
 *   - GroupCallManager — mesh topology for group calls
 *   - QualityController — adaptive quality monitoring
 *
 * Exposes a clean, high-level API consumed by the Zustand call store.
 * All socket signaling flows through this engine.
 */

import { socketManager } from '../socket.manager';
import { CallStateMachine, CallStatus, CallEvent } from './CallStateMachine';
import { MediaDeviceManager, MediaConstraintOptions } from './MediaDeviceManager';
import { PeerConnection, SignalMessage } from './PeerConnection';
import { GroupCallManager, GroupParticipant } from './GroupCallManager';
import { QualityController, QualityChangeEvent, QualityLevel } from './QualityController';
import { ReconnectionManager, GroupReconnectionManager } from './ReconnectionManager';
import { TopologyCoordinator, type CallRoutingMode } from './TopologyCoordinator';
import { MediasoupSFUAdapter } from './MediasoupSFUAdapter';
import { getIceConfig } from './iceConfigService';
import { VideoEffectPipeline, type VideoEffect } from './VideoEffectPipeline';
import { LiveTranscriber } from './LiveTranscriber';

// ── Public Types ────────────────────────────────────

export type CallType = 'audio' | 'video';
export type CallRouting = 'p2p' | 'mesh';

/**
 * Comprehensive call statistics snapshot at a point in time.
 */
export interface CallStatsSnapshot {
  duration: number; // ms
  bytesReceived: number;
  bytesSent: number;
  bitrateMbps: {
    audio: number;
    video: number;
  };
  packetsLost: number;
  rtt: number; // round-trip time in ms
  jitter: number; // ms
  audioLevel: number; // 0-127
  videoFramerate: number;
  videoBitrate: number;
  videoResolution: { width: number; height: number };
  participants: number;
}

/**
 * Network quality probe result.
 */
export interface NetworkProbeResult {
  latency: number; // ms
  quality: 'excellent' | 'good' | 'fair' | 'poor';
  timestamp: number;
}

/**
 * Accumulated call analytics.
 */
export interface CallAnalytics {
  callId: string;
  type: CallType;
  routing: CallRouting;
  startedAt: number;
  endedAt: number | null;
  duration: number; // ms
  totalBytesSent: number;
  totalBytesReceived: number;
  peakBitrate: number; // Mbps
  averageBitrate: number; // Mbps
  qualityChanges: Array<{ timestamp: number; from: QualityLevel; to: QualityLevel }>;
  errors: Array<{ timestamp: number; message: string }>;
  participantCount: number;
  wasMuted: boolean;
  wasVideoOff: boolean;
  hadScreenShare: boolean;
}

export interface CallEngineState {
  callId: string | null;
  status: CallStatus;
  type: CallType;
  routing: CallRouting;
  isInitiator: boolean;
  localUserId: string;
  remoteUserId: string | null;    // For 1-to-1
  channelId: string | null;       // For group
  localStream: MediaStream | null;
  remoteStreams: Map<string, MediaStream>;
  participants: Map<string, GroupParticipant>;
  isMuted: boolean;
  isVideoOff: boolean;
  isScreenSharing: boolean;
  screenStream: MediaStream | null;
  qualityLevel: QualityLevel;
  startedAt: number | null;
  error: string | null;
  isOnHold: boolean;
  isHandRaised: boolean;
}

export interface CallEngineCallbacks {
  onStateChange: (state: CallEngineState) => void;
  onIncomingCall: (data: {
    callId: string;
    callerId: string;
    callerName: string;
    mediaType: CallType;
    channelId?: string;
  }) => void;
  onCallEnded: (reason: string) => void;
  onError: (error: string) => void;
  onParticipantJoined: (participant: GroupParticipant) => void;
  onParticipantLeft: (peerId: string) => void;
  onQualityChange: (event: QualityChangeEvent) => void;
  /** Fired when the server promotes a new host (initiator left). */
  onHostChanged?: (data: {
    callId: string;
    oldHost: string;
    newHost: string;
  }) => void;
  /** Fired on moderation actions targeting THIS user (force-mute, kick). */
  onModerationEvent?: (data: {
    type: 'force_muted' | 'kicked';
    callId: string;
    byUserId?: string;
    muted?: boolean;
    reason?: string;
  }) => void;
  /** Fired when any participant sends a reaction emoji during the call. */
  onReaction?: (data: {
    callId: string;
    userId: string;
    emoji: string;
    ts: number;
  }) => void;
  /** Fired when the server returns a live caption for a chunk. */
  onCaption?: (data: {
    callId: string;
    userId: string;
    text: string;
    language?: string;
    ts: number;
  }) => void;
}

// ── Timeouts ────────────────────────────────────────

const RING_TIMEOUT_MS = 30_000;
const CONNECT_TIMEOUT_MS = 15_000;
const RECONNECT_TIMEOUT_MS = 30_000;  // Max time in reconnecting state

// ── Engine ──────────────────────────────────────────

export class CallEngine {
  private fsm: CallStateMachine;
  private deviceManager: MediaDeviceManager;
  private qualityController: QualityController;
  private callbacks: CallEngineCallbacks;

  // Active call resources
  private peerConnection: PeerConnection | null = null;
  private groupManager: GroupCallManager | null = null;
  private screenSenders: Map<string, RTCRtpSender> = new Map();

  // Reconnection management
  private reconnectionManager: ReconnectionManager | null = null;
  private groupReconnectionManager: GroupReconnectionManager | null = null;

  // Hybrid topology + keepalive
  private topologyCoordinator: TopologyCoordinator | null = null;
  private _callHeartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private static readonly CALL_HEARTBEAT_INTERVAL_MS = 20_000;

  // Recording
  private _mediaRecorder: MediaRecorder | null = null;
  private _recordingChunks: Blob[] = [];
  private _recordingAudioContext: AudioContext | null = null;

  // Analytics
  private _analyticsData: {
    qualityChanges: Array<{ timestamp: number; from: QualityLevel; to: QualityLevel }>;
    errors: Array<{ timestamp: number; message: string }>;
    peakBitrate: number;
    averageBitrates: number[];
  } = {
    qualityChanges: [],
    errors: [],
    peakBitrate: 0,
    averageBitrates: [],
  };

  // State
  private _state: CallEngineState;
  // Audit fix W2: a single _ringTimer was shared between OUTGOING
  // (we initiated, ringing the callee) and INCOMING (we're being
  // rung). An incoming call mid-outgoing-ring leaked the first timer
  // and could fire a stale TIMEOUT against the wrong call. Split.
  private _outgoingRingTimer: ReturnType<typeof setTimeout> | null = null;
  private _incomingRingTimer: ReturnType<typeof setTimeout> | null = null;
  private _connectTimer: ReturnType<typeof setTimeout> | null = null;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _socketUnsubs: Array<() => void> = [];
  private _destroyed = false;
  private _isOnHold = false;
  private _autoQualityEnabled = false;
  // Video effect (blur / darken) pipeline. Lazily allocated when
  // setVideoEffect() is first called with a non-'none' value.
  private _videoEffectPipeline: VideoEffectPipeline | null = null;
  private _currentVideoEffect: VideoEffect = 'none';
  // Live transcription — slices local mic into ~3s chunks and
  // posts each to whisper-cli on the server. Off by default.
  private _liveTranscriber: LiveTranscriber | null = null;
  private _liveCaptionsEnabled = false;
  private _participantVolumes: Map<string, number> = new Map();
  // Monotonic sequence for network_probe so server can correlate
  // out-of-order responses (UDP fan-out, retried emits).
  private _probeSequence = 0;
  // Cached previous getStats() sample for delta-based bitrate calc.
  private _lastStatsSample: {
    bytesReceived: number;
    bytesSent: number;
    framesDecoded: number;
    framesSent: number;
    inboundTs: number;
    outboundTs: number;
  } | null = null;

  constructor(localUserId: string, callbacks: CallEngineCallbacks) {
    this.fsm = new CallStateMachine();
    this.deviceManager = new MediaDeviceManager();
    this.qualityController = new QualityController();
    this.callbacks = callbacks;

    this._state = {
      callId: null,
      status: 'idle',
      type: 'audio',
      routing: 'p2p',
      isInitiator: false,
      localUserId,
      remoteUserId: null,
      channelId: null,
      localStream: null,
      remoteStreams: new Map(),
      participants: new Map(),
      isMuted: false,
      isVideoOff: false,
      isScreenSharing: false,
      screenStream: null,
      qualityLevel: 'excellent',
      startedAt: null,
      error: null,
      isOnHold: false,
      isHandRaised: false,
    };

    // FSM state change → sync engine state
    this.fsm.onChange((prev, next, event) => {
      this._state.status = next;
      this._emitState();

      if (next === 'ended') {
        this._cleanup(event === 'ERROR' ? 'error' : event === 'TIMEOUT' ? 'timeout' : 'ended');
      }

      // Start reconnecting timeout — prevent infinite reconnecting state
      if (next === 'reconnecting') {
        this._startReconnectTimer();
      } else {
        this._clearReconnectTimer();
      }
    });

    // Quality change listener
    this.qualityController.onChange((event) => {
      const oldLevel = this._state.qualityLevel;
      this._state.qualityLevel = event.overallLevel;

      // Track quality changes for analytics
      if (oldLevel !== event.overallLevel) {
        this._analyticsData.qualityChanges.push({
          timestamp: Date.now(),
          from: oldLevel,
          to: event.overallLevel,
        });
      }

      this.callbacks.onQualityChange(event);
      this._emitState();
    });
  }

  get state(): CallEngineState {
    return { ...this._state };
  }

  get mediaDevices(): MediaDeviceManager {
    return this.deviceManager;
  }

  // ── Initialization ────────────────────────────────

  /**
   * Initialize the engine — enumerate devices, register socket listeners.
   * Call once after login.
   */
  async init(): Promise<void> {
    await this.deviceManager.init();
    this._registerSocketListeners();
  }

  /**
   * Full teardown — destroy all resources and unregister listeners.
   * Call on logout. Uses try-finally to guarantee listener cleanup.
   */
  destroy(): void {
    this._destroyed = true;
    try {
      this._cleanup('destroyed');
    } catch (e) {
      console.error('[CallEngine] cleanup error during destroy:', e);
    }
    try {
      this.deviceManager.destroy();
    } catch (e) {
      console.error('[CallEngine] deviceManager destroy error:', e);
    }
    try {
      this.qualityController.destroy();
    } catch (e) {
      console.error('[CallEngine] qualityController destroy error:', e);
    }
    if (this._videoEffectPipeline) {
      try { this._videoEffectPipeline.destroy(); } catch (e) {
        console.error('[CallEngine] videoEffectPipeline destroy error:', e);
      }
      this._videoEffectPipeline = null;
      this._currentVideoEffect = 'none';
    }
    if (this._liveTranscriber) {
      try { this._liveTranscriber.stop(); } catch (e) {
        console.error('[CallEngine] liveTranscriber stop error:', e);
      }
      this._liveTranscriber = null;
      this._liveCaptionsEnabled = false;
    }
    // Always unregister listeners, even if above fails
    this._unregisterSocketListeners();
    this.fsm.removeAllListeners();
  }

  /** Expose QualityController so UI (QualitySelector) can bind to it. */
  getQualityController(): QualityController {
    return this.qualityController;
  }

  // ── Outgoing Call ─────────────────────────────────

  /**
   * Initiate a 1-to-1 call.
   */
  async initiateCall(
    remoteUserId: string,
    mediaType: CallType
  ): Promise<void> {
    if (this.fsm.state !== 'idle') {
      this.callbacks.onError('Cannot start call: already in a call');
      return;
    }

    this.fsm.transition('INITIATE');

    this._state.type = mediaType;
    this._state.routing = 'p2p';
    this._state.isInitiator = true;
    this._state.remoteUserId = remoteUserId;
    this._state.channelId = null;

    try {
      // Acquire local media
      const stream = await this.deviceManager.acquireLocalStream({
        audio: true,
        video: mediaType === 'video',
      });
      this._state.localStream = stream;

      // Notify server (v2 event names match backend handlers)
      const response = await socketManager.emit('v2_call_initiate', {
        target_id: remoteUserId,
        media_type: mediaType,
      });

      this._state.callId = response?.call_id || `call_${Date.now()}`;

      // Start ring timeout (outgoing variant)
      this._clearOutgoingRingTimer();
      this._outgoingRingTimer = setTimeout(() => {
        if (this.fsm.state === 'ringing') {
          this.fsm.transition('TIMEOUT');
        }
      }, RING_TIMEOUT_MS);

      this._emitState();
    } catch (e: any) {
      console.error('[CallEngine] initiateCall error:', e);
      this.fsm.transition('ERROR');
      this.callbacks.onError(e.message || 'Failed to initiate call');
    }
  }

  /**
   * Initiate a group call in a channel.
   */
  async initiateGroupCall(
    channelId: string,
    mediaType: CallType
  ): Promise<void> {
    if (this.fsm.state !== 'idle') {
      this.callbacks.onError('Cannot start group call: already in a call');
      return;
    }

    this.fsm.transition('INITIATE');

    this._state.type = mediaType;
    this._state.routing = 'mesh';
    this._state.isInitiator = true;
    this._state.remoteUserId = null;
    this._state.channelId = channelId;

    try {
      const stream = await this.deviceManager.acquireLocalStream({
        audio: true,
        video: mediaType === 'video',
      });
      this._state.localStream = stream;

      const response = await socketManager.emit('v2_call_join_group', {
        channel_id: channelId,
        media_type: mediaType,
      });

      this._state.callId = response?.call_id || `gcall_${Date.now()}`;

      // Create group manager
      this._createGroupManager();

      // Transition to connecting (group calls don't ring)
      this.fsm.transition('PEER_READY');

      // Audit fix #8b: previously the FSM advanced to CONNECTED only
      // on FIRST REMOTE STREAM. That broke the very common case where
      // the host joins a channel call alone and waits for others —
      // the connect-timeout would fire after CONNECT_TIMEOUT_MS and
      // tear down the perfectly-valid call.
      //
      // Behaviour now: group calls treat a successful server-side
      // join as enough to transition to CONNECTED. The connect timer
      // is only armed when we EXPECT remote peers (response carries
      // existing participants > self). Streams arriving later still
      // trigger the same `onRemoteStream` path; no double-transition
      // because canTransition('CONNECTED') will be false the second
      // time.
      const otherPartipants = (response?.participants || [])
        .filter((p: any) => p?.user_id && p.user_id !== this._state.localUserId);

      if (otherPartipants.length === 0) {
        // Alone in the call — transition immediately so the host UI
        // shows "active" with a waiting-for-others banner instead of
        // a phantom "connecting" → "timeout" cycle.
        if (this.fsm.canTransition('CONNECTED')) {
          this.fsm.transition('CONNECTED');
          this._state.startedAt = Date.now();
        }
      } else {
        // Others are already in the call — start the connect timer
        // so a never-arriving remote stream surfaces a real timeout.
        this._startConnectTimer();
        for (const p of otherPartipants) {
          this.groupManager!.addParticipant(
            p.user_id,
            this._state.localUserId < p.user_id, // consistent initiator tiebreaker
          );
        }
      }

      this._emitState();
    } catch (e: any) {
      console.error('[CallEngine] initiateGroupCall error:', e);
      this.fsm.transition('ERROR');
      this.callbacks.onError(e.message || 'Failed to start group call');
    }
  }

  // ── Incoming Call ─────────────────────────────────

  /**
   * Accept an incoming call.
   */
  async acceptCall(): Promise<void> {
    if (!this.fsm.canTransition('ACCEPT')) return;

    this.fsm.transition('ACCEPT');
    this._clearRingTimer();

    try {
      const stream = await this.deviceManager.acquireLocalStream({
        audio: true,
        video: this._state.type === 'video',
      });
      this._state.localStream = stream;

      if (this._state.routing === 'mesh' && this._state.channelId) {
        // Group call: join the room so the server pairs us with existing
        // participants and fans out call_participant_joined to everyone.
        const response = await socketManager.emit('v2_call_join_group', {
          channel_id: this._state.channelId,
          media_type: this._state.type,
          call_id: this._state.callId,
        });

        this._createGroupManager();

        // Seed the mesh with participants that were already in the call.
        // Lexicographic tiebreaker: lower user_id is the initiator of each pair.
        if (response?.participants && Array.isArray(response.participants)) {
          for (const p of response.participants) {
            const pid = p.user_id;
            if (pid && pid !== this._state.localUserId) {
              this.groupManager!.addParticipant(
                pid,
                this._state.localUserId < pid
              );
            }
          }
        }
      } else if (this._state.routing === 'p2p' && this._state.remoteUserId) {
        // 1-to-1: ack the caller, then wait for their offer to arrive.
        await socketManager.emit('v2_call_accept', {
          call_id: this._state.callId,
          caller_id: this._state.remoteUserId,
        });
        await this._createPeerConnection(this._state.remoteUserId, false);
      }

      this._startConnectTimer();
      this._emitState();
    } catch (e: any) {
      console.error('[CallEngine] acceptCall error:', e);
      this.fsm.transition('ERROR');
      this.callbacks.onError(e.message || 'Failed to accept call');
    }
  }

  /**
   * Reject an incoming call.
   */
  rejectCall(): void {
    if (!this.fsm.canTransition('REJECT')) return;

    socketManager.emitNoAck('v2_call_reject', {
      call_id: this._state.callId,
      caller_id: this._state.remoteUserId,
    });

    this.fsm.transition('REJECT');
  }

  /**
   * Hang up the current call.
   */
  hangup(): void {
    if (!this.fsm.isLive) return;

    if (this._state.routing === 'mesh' && this._state.channelId) {
      socketManager.emitNoAck('v2_call_leave_group', {
        call_id: this._state.callId,
        channel_id: this._state.channelId,
      });
    } else {
      socketManager.emitNoAck('v2_call_hangup', {
        call_id: this._state.callId,
        target_id: this._state.remoteUserId,
      });
    }

    this.fsm.transition('HANGUP');
  }

  // ── Media Controls ────────────────────────────────

  toggleMute(): void {
    this._state.isMuted = !this._state.isMuted;
    this.deviceManager.muteAudio(this._state.isMuted);

    socketManager.emitNoAck('v2_call_toggle_mute', {
      call_id: this._state.callId,
      muted: this._state.isMuted,
    });

    // In SFU mode the local track-disable alone keeps silent packets flowing.
    // Pausing the producer on the SFU frees real downlink bandwidth for every
    // consumer and lets remote UIs grey out the tile instantly.
    this._syncSFUProducer('audio', this._state.isMuted);

    this._emitState();
  }

  toggleVideo(): void {
    this._state.isVideoOff = !this._state.isVideoOff;
    this.deviceManager.muteVideo(this._state.isVideoOff);

    socketManager.emitNoAck('v2_call_toggle_video', {
      call_id: this._state.callId,
      video_off: this._state.isVideoOff,
    });

    this._syncSFUProducer('video', this._state.isVideoOff);

    this._emitState();
  }

  /**
   * Apply a video effect (blur / darken / none) to the outgoing
   * video track. Routes the local stream through a canvas-based
   * effect pipeline and replaces the track on every sender. When
   * switching back to 'none', the original camera track is
   * restored. Audio is untouched.
   */
  async setVideoEffect(effect: VideoEffect): Promise<void> {
    if (!this._state.localStream) return;

    const localStream = this._state.localStream;
    const originalVideoTrack = localStream.getVideoTracks()[0];
    if (!originalVideoTrack) return;

    if (effect === 'none') {
      // Restore the raw camera track and tear the pipeline down so
      // we stop spending CPU on canvas redraws.
      if (this._videoEffectPipeline) {
        try {
          await this._replaceLocalVideoTrack(originalVideoTrack);
        } catch (e) {
          console.warn('[CallEngine] restoreVideoTrack failed:', e);
        }
        this._videoEffectPipeline.destroy();
        this._videoEffectPipeline = null;
      }
      this._currentVideoEffect = 'none';
      return;
    }

    // Lazily build the pipeline against the current local stream.
    if (!this._videoEffectPipeline) {
      this._videoEffectPipeline = new VideoEffectPipeline(localStream);
    } else {
      this._videoEffectPipeline.setInput(localStream);
    }
    this._videoEffectPipeline.setEffect(effect);
    this._currentVideoEffect = effect;

    const processed = this._videoEffectPipeline.outputStream.getVideoTracks()[0];
    if (processed) {
      try { await this._replaceLocalVideoTrack(processed); } catch (e) {
        console.warn('[CallEngine] swap to processed track failed:', e);
      }
    }
  }

  /** Currently-applied video effect (read-only mirror). */
  get currentVideoEffect(): VideoEffect {
    return this._currentVideoEffect;
  }

  /**
   * Load a custom background image into the video effect pipeline.
   * The image is decoded on the renderer's main thread and then
   * used as the background layer when ``setVideoEffect('image')``
   * is invoked. Pass empty string to clear.
   */
  async setVideoBackgroundImage(src: string): Promise<void> {
    if (!this._state.localStream) return;
    if (!this._videoEffectPipeline) {
      this._videoEffectPipeline = new VideoEffectPipeline(this._state.localStream);
    }
    await this._videoEffectPipeline.setBackgroundImage(src);
  }

  private async _replaceLocalVideoTrack(track: MediaStreamTrack): Promise<void> {
    if (this.peerConnection && !this.peerConnection.destroyed) {
      try { await this.peerConnection.replaceTrack(track); } catch (e) {
        console.warn('[CallEngine] replaceLocalVideoTrack p2p failed:', e);
      }
    }
    if (this.groupManager && !this.groupManager.destroyed) {
      try { await this.groupManager.replaceTrackAll(track); } catch (e) {
        console.warn('[CallEngine] replaceLocalVideoTrack mesh failed:', e);
      }
    }
    const sfu = this.topologyCoordinator?.sfu;
    if (sfu?.replaceTrack) {
      sfu.replaceTrack('video', track).catch((err: any) =>
        console.warn('[CallEngine] SFU replaceTrack(video) failed:', err),
      );
    }
  }

  /**
   * Toggle live captions for the local user. When enabled, slices
   * the mic into ~3s chunks and posts each to the server's whisper
   * worker. Captions are broadcast via ``call:caption`` to every
   * participant, so a single user enabling captions transcribes
   * THEIR speech for everyone — useful for one-to-many briefings.
   */
  setLiveCaptions(enabled: boolean): void {
    if (!this.fsm.isLive || !this._state.callId || !this._state.localStream) {
      return;
    }
    if (enabled === this._liveCaptionsEnabled) return;
    this._liveCaptionsEnabled = enabled;

    if (enabled) {
      this._liveTranscriber = new LiveTranscriber(
        this._state.localStream, this._state.callId,
      );
      this._liveTranscriber.start();
    } else {
      try { this._liveTranscriber?.stop(); } catch { /* ignore */ }
      this._liveTranscriber = null;
    }
  }

  get liveCaptionsEnabled(): boolean {
    return this._liveCaptionsEnabled;
  }

  /**
   * Send a transient reaction emoji to every call participant.
   * Reactions are fire-and-forget — they float up the receiver's
   * screen for ~2s and disappear (no persistence).
   */
  sendReaction(emoji: string): void {
    if (!this.fsm.isLive || !this._state.callId) return;
    const cleaned = (emoji || '').slice(0, 16);
    if (!cleaned) return;

    socketManager.emitNoAck('v2_call_reaction', {
      call_id: this._state.callId,
      emoji: cleaned,
    });
  }

  /**
   * Toggle the local user's raise-hand flag and broadcast to peers.
   * Webinar feature: audience raises hand → host sees indicator and
   * grants the floor (typically by unmuting / spotlighting them).
   */
  toggleHand(): void {
    if (!this.fsm.isLive || !this._state.callId) return;

    this._state.isHandRaised = !this._state.isHandRaised;

    socketManager.emitNoAck('v2_call_toggle_hand', {
      call_id: this._state.callId,
      raised: this._state.isHandRaised,
    });

    // Mirror locally so the UI shows the hand on the local tile
    // immediately, without waiting for the round-trip echo.
    if (this.groupManager) {
      this.groupManager.updateParticipantState(this._state.localUserId, {
        isHandRaised: this._state.isHandRaised,
        handRaisedAt: this._state.isHandRaised
          ? new Date().toISOString()
          : null,
      });
    }

    this._emitState();
  }

  /**
   * Fire SFU producer pause/resume when we're routing through the SFU. A
   * pure-mesh call has no producers, so ``affected`` will be 0 and the call
   * is effectively a no-op — safe to call unconditionally.
   */
  private _syncSFUProducer(kind: 'audio' | 'video', paused: boolean): void {
    const adapter = this.topologyCoordinator?.sfu;
    if (!adapter?.setProducerPaused) return;
    adapter.setProducerPaused(kind, paused).catch((err) => {
      console.warn(`[CallEngine] SFU ${kind} ${paused ? 'pause' : 'resume'} failed:`, err);
    });
  }

  /**
   * Audit fix: noise suppression in call.store.v2 needs to push the
   * processed track to every Sender, not just the local stream.
   * Reuses the same replaceTrack plumbing as switchAudioInput.
   */
  async replaceLocalAudioTrack(newTrack: MediaStreamTrack): Promise<void> {
    if (!newTrack) return;
    if (this.peerConnection && !this.peerConnection.destroyed) {
      try { await this.peerConnection.replaceTrack(newTrack); } catch (e) {
        console.warn('[CallEngine] replaceLocalAudioTrack p2p failed:', e);
      }
    }
    if (this.groupManager && !this.groupManager.destroyed) {
      try { await this.groupManager.replaceTrackAll(newTrack); } catch (e) {
        console.warn('[CallEngine] replaceLocalAudioTrack mesh failed:', e);
      }
    }
    const sfu = this.topologyCoordinator?.sfu;
    if (sfu?.replaceTrack) {
      sfu.replaceTrack('audio', newTrack).catch((err) =>
        console.warn('[CallEngine] SFU replaceTrack(audio) failed:', err),
      );
    }
  }

  async switchAudioInput(deviceId: string): Promise<void> {
    const newTrack = await this.deviceManager.switchAudioInput(deviceId);
    if (!newTrack) return;

    // Propagate to peer connections
    if (this.peerConnection && !this.peerConnection.destroyed) {
      await this.peerConnection.replaceTrack(newTrack);
    }
    if (this.groupManager && !this.groupManager.destroyed) {
      await this.groupManager.replaceTrackAll(newTrack);
    }
    // When the call is routed through the SFU (large groups / cross-server
    // federation), mesh replaceTrack() reaches nobody — the new track has
    // to be swapped on the upstream producer instead.
    const sfu = this.topologyCoordinator?.sfu;
    if (sfu?.replaceTrack) {
      sfu.replaceTrack('audio', newTrack).catch((err) =>
        console.warn('[CallEngine] SFU replaceTrack(audio) failed:', err),
      );
    }
  }

  async switchVideoInput(deviceId: string): Promise<void> {
    const newTrack = await this.deviceManager.switchVideoInput(deviceId);
    if (!newTrack) return;

    if (this.peerConnection && !this.peerConnection.destroyed) {
      await this.peerConnection.replaceTrack(newTrack);
    }
    if (this.groupManager && !this.groupManager.destroyed) {
      await this.groupManager.replaceTrackAll(newTrack);
    }
    const sfu = this.topologyCoordinator?.sfu;
    if (sfu?.replaceTrack) {
      sfu.replaceTrack('video', newTrack).catch((err) =>
        console.warn('[CallEngine] SFU replaceTrack(video) failed:', err),
      );
    }
  }

  async setAudioOutput(deviceId: string, element: HTMLMediaElement): Promise<void> {
    this.deviceManager.setAudioOutput(deviceId);
    await this.deviceManager.setOutputDevice(element);
  }

  // ── Screen Sharing ────────────────────────────────

  async startScreenShare(sourceId: string): Promise<void> {
    if (this._state.isScreenSharing) return;

    try {
      const screenStream = await this.deviceManager.acquireScreenStream(sourceId);
      const screenTrack = screenStream.getVideoTracks()[0];

      if (!screenTrack) throw new Error('No video track from screen capture');

      this._state.isScreenSharing = true;
      this._state.screenStream = screenStream;

      // Add track to peer connections
      if (this.peerConnection && !this.peerConnection.destroyed) {
        const sender = this.peerConnection.addScreenTrack(screenTrack, screenStream);
        this.screenSenders.set('single', sender);
      }

      if (this.groupManager && !this.groupManager.destroyed) {
        const senders = this.groupManager.addScreenTrackAll(screenTrack, screenStream);
        for (const [peerId, sender] of senders) {
          this.screenSenders.set(peerId, sender);
        }
      }

      // When the call has switched to SFU routing (large groups), the mesh
      // track additions above reach nobody — every participant consumes via
      // the SFU. Publish the screen stream as a tagged producer so remote
      // peers get a dedicated tile.
      const sfuAdapter = this.topologyCoordinator?.sfu;
      if (sfuAdapter?.publishScreenShare) {
        sfuAdapter.publishScreenShare(screenStream).catch((err) => {
          console.warn('[CallEngine] SFU screen publish failed:', err);
        });
      }

      // Listen for track ended (user stops sharing via OS UI)
      screenTrack.onended = () => {
        this.stopScreenShare();
      };

      socketManager.emitNoAck('v2_call_screen_share_start', {
        call_id: this._state.callId,
      });

      this._emitState();
    } catch (e: any) {
      console.error('[CallEngine] startScreenShare error:', e);
      this.callbacks.onError(e.message || 'Failed to start screen sharing');
    }
  }

  stopScreenShare(): void {
    if (!this._state.isScreenSharing) return;

    // Remove track from peer connections
    if (this.peerConnection && !this.peerConnection.destroyed) {
      const sender = this.screenSenders.get('single');
      if (sender) this.peerConnection.removeScreenTrack(sender);
    }

    if (this.groupManager && !this.groupManager.destroyed) {
      this.groupManager.removeScreenTrackAll(this.screenSenders);
    }

    // Mirror the teardown on the SFU side if we were publishing there.
    const sfuAdapter = this.topologyCoordinator?.sfu;
    if (sfuAdapter?.unpublishScreenShare) {
      sfuAdapter.unpublishScreenShare().catch((err) => {
        console.warn('[CallEngine] SFU screen unpublish failed:', err);
      });
    }

    this.screenSenders.clear();
    this.deviceManager.releaseScreenStream();

    this._state.isScreenSharing = false;
    this._state.screenStream = null;

    socketManager.emitNoAck('v2_call_screen_share_stop', {
      call_id: this._state.callId,
    });

    this._emitState();
  }

  // ── Quality Controls ──────────────────────────────

  async setQualityPreset(preset: string): Promise<void> {
    await this.qualityController.forcePreset(preset);
  }

  // ── Audio Level ───────────────────────────────────

  /**
   * Create an audio level monitor for a stream.
   * Returns { getLevel, stop } — call getLevel() on animation frame for VU meter.
   */
  createAudioLevelMonitor(stream: MediaStream) {
    return this.deviceManager.createAudioLevelMonitor(stream);
  }

  // ── Call Hold/Resume ──────────────────────────────

  /**
   * Hold the current call — disable all tracks and notify server.
   */
  async holdCall(): Promise<void> {
    if (this._isOnHold || !this.fsm.isLive) return;

    try {
      // Disable all local tracks
      if (this._state.localStream) {
        for (const track of this._state.localStream.getTracks()) {
          track.enabled = false;
        }
      }

      this._isOnHold = true;
      this._state.isOnHold = true;

      // Notify remote
      socketManager.emitNoAck('v2_call_hold', {
        call_id: this._state.callId,
      });

      this._emitState();
    } catch (e: any) {
      console.error('[CallEngine] holdCall error:', e);
      this.callbacks.onError(e.message || 'Failed to hold call');
    }
  }

  /**
   * Resume a held call — re-enable all tracks and notify server.
   */
  async resumeCall(): Promise<void> {
    if (!this._isOnHold || !this.fsm.isLive) return;

    try {
      // Re-enable local tracks
      if (this._state.localStream) {
        for (const track of this._state.localStream.getTracks()) {
          // Respect mute state
          if (track.kind === 'audio') {
            track.enabled = !this._state.isMuted;
          } else if (track.kind === 'video') {
            track.enabled = !this._state.isVideoOff;
          }
        }
      }

      this._isOnHold = false;
      this._state.isOnHold = false;

      // Notify remote
      socketManager.emitNoAck('v2_call_resume', {
        call_id: this._state.callId,
      });

      this._emitState();
    } catch (e: any) {
      console.error('[CallEngine] resumeCall error:', e);
      this.callbacks.onError(e.message || 'Failed to resume call');
    }
  }

  // ── Call Statistics ──────────────────────────────

  /**
   * Get a comprehensive call statistics snapshot.
   *
   * Audit fix: bitrate / frameRate were dividing the cumulative
   * bytes/frames by the cumulative timestamp (= ms since unix epoch
   * for stats reports, OR a monotonic origin depending on browser).
   * Either way, after a few seconds the divisor is huge and the
   * resulting "bitrate" is essentially 0 mbps. Now we cache the
   * previous sample and compute deltas across calls.
   */
  async getCallStats(): Promise<CallStatsSnapshot> {
    const stats: CallStatsSnapshot = {
      duration: this._state.startedAt ? Date.now() - this._state.startedAt : 0,
      bytesReceived: 0,
      bytesSent: 0,
      bitrateMbps: { audio: 0, video: 0 },
      packetsLost: 0,
      rtt: 0,
      jitter: 0,
      audioLevel: 0,
      videoFramerate: 0,
      videoBitrate: 0,
      videoResolution: { width: 0, height: 0 },
      participants: this._state.participants.size,
    };

    // Gather stats from peer connection(s)
    if (this.peerConnection) {
      const rtcStats = await this.peerConnection.getStats();
      let bytesReceived = 0;
      let bytesSent = 0;
      let packetsLost = 0;
      let rtt = 0;
      let jitter = 0;
      let framesDecoded = 0;
      let framesSent = 0;
      let inboundTs = 0;
      let outboundTs = 0;

      rtcStats.forEach((report) => {
        if (report.type === 'inbound-rtp' && report.mediaType === 'video') {
          bytesReceived = report.bytesReceived || 0;
          packetsLost = report.packetsLost || 0;
          jitter = report.jitter || 0;
          framesDecoded = report.framesDecoded || 0;
          inboundTs = report.timestamp || 0;
        } else if (report.type === 'outbound-rtp' && report.mediaType === 'video') {
          bytesSent = report.bytesSent || 0;
          framesSent = report.framesSent || 0;
          outboundTs = report.timestamp || 0;
        } else if (report.type === 'candidate-pair' && report.state === 'succeeded') {
          rtt = report.currentRoundTripTime || 0;
        }
      });

      // Delta-based rate calc (audit fix). Falls back to 0 on the
      // first sample (no prior point to compare against).
      const prev = this._lastStatsSample;
      let frameRate = 0;
      let videoBitrate = 0;
      if (prev) {
        const inboundDtSec = (inboundTs - prev.inboundTs) / 1000;
        const outboundDtSec = (outboundTs - prev.outboundTs) / 1000;
        if (inboundDtSec > 0) {
          frameRate = Math.max(0, framesDecoded - prev.framesDecoded) / inboundDtSec;
        }
        if (outboundDtSec > 0) {
          videoBitrate =
            (Math.max(0, bytesSent - prev.bytesSent) * 8) / outboundDtSec;
        }
      }
      this._lastStatsSample = {
        bytesReceived, bytesSent, framesDecoded, framesSent,
        inboundTs, outboundTs,
      };

      stats.bytesReceived = bytesReceived;
      stats.bytesSent = bytesSent;
      stats.packetsLost = packetsLost;
      stats.rtt = rtt;
      stats.jitter = jitter;
      stats.videoFramerate = frameRate;
      stats.videoBitrate = videoBitrate;
      stats.videoResolution = { width: 0, height: 0 };
    }

    if (this.groupManager) {
      const groupStats = await (this.groupManager as any).getAggregateMetrics?.();
      if (groupStats) {
        stats.bytesReceived = (stats.bytesReceived || 0) + (groupStats.totalBytesReceived || 0);
        stats.bytesSent = (stats.bytesSent || 0) + (groupStats.totalBytesSent || 0);
      }
    }

    return stats;
  }

  // ── Network Quality Probe ─────────────────────────

  /**
   * Run a quick network quality check (ping-like latency test via data channel or timing).
   */
  async probeNetworkQuality(): Promise<NetworkProbeResult> {
    const startTime = Date.now();

    try {
      // Audit fix #8c: payload was `{ timestamp }` but the server's
      // network_probe handler expects `{ call_id, client_timestamp_ms,
      // sequence }`. Wrong shape made the server reject ALL probes
      // with "call_id required", so probeNetworkQuality has been
      // returning fallback "poor" forever. Now matches the server
      // contract in app/socket/call_handlers.py:2449.
      const probeData = await socketManager.emit('network_probe', {
        call_id: this._state.callId,
        client_timestamp_ms: startTime,
        sequence: ++this._probeSequence,
      });

      const latency = Date.now() - startTime;
      let quality: 'excellent' | 'good' | 'fair' | 'poor';

      if (latency < 50) {
        quality = 'excellent';
      } else if (latency < 100) {
        quality = 'good';
      } else if (latency < 200) {
        quality = 'fair';
      } else {
        quality = 'poor';
      }

      return {
        latency,
        quality,
        timestamp: Date.now(),
      };
    } catch (e: any) {
      console.error('[CallEngine] probeNetworkQuality error:', e);
      // Assume poor quality on error
      return {
        latency: 500,
        quality: 'poor',
        timestamp: Date.now(),
      };
    }
  }

  // ── Participant Volume Control ────────────────────

  /**
   * Set the volume for a specific participant (0.0 to 1.0).
   *
   * Audit fix: previous implementation built `new AudioContext()`
   * inside a for-loop over audio tracks AND never closed any of
   * them. Each call leaked ~16 MB of audio engine state. The "real
   * implementation" comment was a TODO.
   *
   * Behaviour now: store the clamped volume in `_participantVolumes`
   * and rely on the consumer's `<audio>` element's `volume` attribute
   * (set by ParticipantGrid / CallView via `getParticipantVolume`).
   * No AudioContext created here at all. If we ever need actual
   * GainNode-style mixing for individual peer volumes, a single
   * shared context can be lazy-created elsewhere with proper
   * lifecycle.
   */
  setParticipantVolume(peerId: string, volume: number): void {
    const clampedVolume = Math.max(0, Math.min(1, volume));
    this._participantVolumes.set(peerId, clampedVolume);
    // Trigger a state emit so any subscribed component (audio
    // element wrappers, volume sliders) re-renders with the new
    // value.
    this._emitState();
  }

  /**
   * Get the current volume for a participant.
   */
  getParticipantVolume(peerId: string): number {
    return this._participantVolumes.get(peerId) ?? 1.0;
  }

  // ── Call Recording ───────────────────────────────

  /**
   * Start recording the local call (MediaRecorder on combined streams).
   */
  async startLocalRecording(): Promise<void> {
    if (this._mediaRecorder && this._mediaRecorder.state === 'recording') return;

    try {
      if (!this._state.localStream) {
        throw new Error('No local stream available for recording');
      }

      const AudioCtx = (window as any).AudioContext || (window as any).webkitAudioContext;
      if (!AudioCtx) throw new Error('AudioContext not available');
      const audioContext: AudioContext = new AudioCtx();
      this._recordingAudioContext = audioContext;
      const audioDestination = audioContext.createMediaStreamDestination();

      // Wire local audio tracks INTO the destination — without this
      // the recording is a silent blob (the previous implementation
      // built the destination but never connected any source).
      const localAudio = this._state.localStream.getAudioTracks();
      for (const t of localAudio) {
        try {
          const src = audioContext.createMediaStreamSource(
            new MediaStream([t]),
          );
          src.connect(audioDestination);
        } catch (err) {
          console.warn('[CallEngine] failed to wire audio track', err);
        }
      }
      const mixedStream = audioDestination.stream;
      if (mixedStream.getAudioTracks().length === 0) {
        throw new Error('No audio tracks available for recording');
      }

      this._mediaRecorder = new MediaRecorder(mixedStream);
      this._recordingChunks = [];

      this._mediaRecorder.ondataavailable = (event) => {
        this._recordingChunks.push(event.data);
      };

      this._mediaRecorder.start();
      console.log('[CallEngine] Recording started');
    } catch (e: any) {
      // Roll back the AudioContext we may have allocated above so we
      // don't leak it on the error path.
      if (this._recordingAudioContext) {
        try { await this._recordingAudioContext.close(); } catch { /* noop */ }
        this._recordingAudioContext = null;
      }
      console.error('[CallEngine] startLocalRecording error:', e);
      this.callbacks.onError(e.message || 'Failed to start recording');
    }
  }

  /**
   * Stop recording and return the recorded data as a Blob.
   */
  async stopLocalRecording(): Promise<Blob> {
    if (!this._mediaRecorder) {
      return new Blob();
    }

    return new Promise((resolve) => {
      const mediaRecorder = this._mediaRecorder;
      if (!mediaRecorder) {
        return resolve(new Blob());
      }

      mediaRecorder.onstop = () => {
        const blob = new Blob(this._recordingChunks, { type: 'audio/webm' });
        this._recordingChunks = [];
        this._mediaRecorder = null;
        // Release the AudioContext + nodes so each recording session
        // doesn't leak ~16 MB of audio engine state.
        if (this._recordingAudioContext) {
          this._recordingAudioContext.close().catch(() => {});
          this._recordingAudioContext = null;
        }
        console.log('[CallEngine] Recording stopped');
        resolve(blob);
      };

      mediaRecorder.stop();
    });
  }

  // ── Auto-Quality Adjustment ──────────────────────

  /**
   * Enable or disable automatic quality adjustment based on network conditions.
   */
  enableAutoQuality(enabled: boolean): void {
    this._autoQualityEnabled = enabled;

    if (enabled) {
      // Quality controller automatically adjusts based on stats
      this.qualityController.start();
    } else {
      // Disable automatic adjustments
      this.qualityController.stop();
    }
  }

  // ── Call Analytics ───────────────────────────────

  /**
   * Get accumulated call analytics data.
   */
  getCallAnalytics(): CallAnalytics {
    const endedAt = this._state.status === 'ended' ? Date.now() : null;
    const duration = this._state.startedAt
      ? (endedAt || Date.now()) - this._state.startedAt
      : 0;

    const averageBitrate =
      this._analyticsData.averageBitrates.length > 0
        ? this._analyticsData.averageBitrates.reduce((a, b) => a + b, 0) /
          this._analyticsData.averageBitrates.length
        : 0;

    return {
      callId: this._state.callId || '',
      type: this._state.type,
      routing: this._state.routing,
      startedAt: this._state.startedAt || 0,
      endedAt,
      duration,
      totalBytesSent: 0,
      totalBytesReceived: 0,
      peakBitrate: this._analyticsData.peakBitrate,
      averageBitrate,
      qualityChanges: this._analyticsData.qualityChanges,
      errors: this._analyticsData.errors,
      participantCount: this._state.participants.size,
      wasMuted: this._state.isMuted,
      wasVideoOff: this._state.isVideoOff,
      hadScreenShare: this._state.isScreenSharing,
    };
  }

  // ── Bandwidth Estimation ─────────────────────────

  /**
   * Estimate available bandwidth (upload and download in Kbps).
   */
  async estimateBandwidth(): Promise<{ uploadKbps: number; downloadKbps: number }> {
    // This is a simplified estimation; real implementations would use
    // BWCE (Bandwidth Constrained Estimation) from WebRTC stats
    try {
      const stats = await this.getCallStats();

      // Very rough estimation based on current bitrate
      const uploadKbps = Math.max(0, (stats.bitrateMbps.audio + stats.bitrateMbps.video) * 1000);
      const downloadKbps = uploadKbps; // Symmetric for P2P

      return {
        uploadKbps,
        downloadKbps,
      };
    } catch (e: any) {
      console.error('[CallEngine] estimateBandwidth error:', e);
      return { uploadKbps: 0, downloadKbps: 0 };
    }
  }

  // ── Reconnection Setup ───────────────────────────

  /**
   * Setup reconnection manager after peer connection is established.
   */
  private _setupReconnectionManager(): void {
    if (this._state.routing === 'p2p' && this._state.remoteUserId) {
      if (this.reconnectionManager) {
        this.reconnectionManager.destroy();
      }

      this.reconnectionManager = new ReconnectionManager({
        peerId: this._state.remoteUserId || 'unknown',
        maxRetries: 5,
        initialBackoffMs: 1000,
        maxBackoffMs: 8000,
        onStateChange: (event) => {
          console.log(`[CallEngine] Reconnection state: ${event.previousState} → ${event.nextState} (${event.reason})`);
          if (event.nextState === 'recovering') {
            if (this.peerConnection && !this.peerConnection.destroyed) {
              (this.peerConnection as any)._attemptIceRestartWithBackoff?.();
            }
          } else if (event.nextState === 'failed') {
            console.error('[CallEngine] Reconnection failed');
            if (this.fsm.canTransition('DISCONNECTED')) {
              this.fsm.transition('DISCONNECTED');
            }
          }
        },
        onRetryAttempt: (attempt) => {
          console.log(`[CallEngine] Reconnection attempt #${attempt.attemptNumber} (backoff: ${attempt.backoffDelayMs}ms)`);
        },
        // Route the manager's internal restart through PeerConnection so
        // the offer is actually signaled to the remote (audit fix —
        // previously the manager called setLocalDescription only).
        onIceRestartRequested: () => {
          if (this.peerConnection && !this.peerConnection.destroyed) {
            (this.peerConnection as any)._attemptIceRestart?.();
          }
        },
      });
    } else if (this._state.routing === 'mesh' && this.groupManager) {
      if (this.groupReconnectionManager) {
        this.groupReconnectionManager.destroy();
      }

      this.groupReconnectionManager = new GroupReconnectionManager({
        maxRetries: 5,
        initialBackoffMs: 1000,
        maxBackoffMs: 8000,
        onStateChange: (event) => {
          console.log(`[CallEngine] Group reconnection: ${event.previousState} → ${event.nextState}`);
          if (event.nextState === 'failed') {
            console.error('[CallEngine] Group reconnection failed');
            if (this.fsm.canTransition('DISCONNECTED')) {
              this.fsm.transition('DISCONNECTED');
            }
          }
        },
      });
    }
  }

  // ── Private: PeerConnection Setup ─────────────────

  // Cached server-issued RTCConfiguration. Populated lazily from
  // /api/turn/ice-config the first time we need it. Keeps us from
  // shipping every new PeerConnection with the legacy LAN-only config
  // — that left cross-NAT users unable to connect because no TURN was
  // present in iceServers.
  private _iceConfig: RTCConfiguration | null = null;

  private async _ensureIceConfig(): Promise<RTCConfiguration | null> {
    try {
      this._iceConfig = await getIceConfig();
      return this._iceConfig;
    } catch {
      return null;  // PeerConnection will fall back to LAN-only static
    }
  }

  private async _createPeerConnection(peerId: string, isInitiator: boolean): Promise<void> {
    if (this.peerConnection && !this.peerConnection.destroyed) {
      this.peerConnection.destroy();
    }

    // Audit fix W1: AWAIT the ICE config fetch instead of fire-and-
    // forget. The legacy "best-effort prefetch" landed AFTER the PC
    // was constructed with iceServers=undefined, so cross-network
    // calls had to wait for the next ICE restart to actually use TURN
    // — which most never reached. Now we block (briefly) so the very
    // first offer carries the right ICE list.
    if (!this._iceConfig) {
      await this._ensureIceConfig();
    }

    this.peerConnection = new PeerConnection(
      {
        peerId,
        isInitiator,
        localStream: this._state.localStream,
        onSignal: (data) => {
          // Tag every signaling emission with sent_at_ms so the server
          // can drop stale ICE candidates that arrive after a topology
          // switch (different ufrag/pwd → DOA on the new PC). The
          // server's default TTL is 10s for ICE candidates, 30s for
          // SDP — both ample for a healthy LAN.
          socketManager.emitNoAck('call_signal', {
            call_id: this._state.callId,
            target_id: data.targetId,
            signal_type: data.type,
            sdp: data.sdp,
            candidate: data.candidate,
            sent_at_ms: Date.now(),
          });
        },
        onRemoteTrack: (_track, _streams) => {
          // Handled via onRemoteStream
        },
        onStateChange: (state) => {
          if (state === 'connected') {
            this._clearConnectTimer();
            if (this.fsm.canTransition('CONNECTED')) {
              this.fsm.transition('CONNECTED');
              this._state.startedAt = Date.now();
              this.qualityController.attachSinglePeer(this.peerConnection!);
              this.qualityController.start();
              // Setup reconnection manager after connection
              this._setupReconnectionManager();
            }
          } else if (state === 'disconnected') {
            if (this.fsm.canTransition('DISCONNECTED')) {
              this.fsm.transition('DISCONNECTED');
            }
          } else if (state === 'failed') {
            if (this.fsm.canTransition('DISCONNECTED')) {
              this.fsm.transition('DISCONNECTED');
            }
          }
        },
        onIceStateChange: (_state) => {},
        onRemoteStream: (stream) => {
          this._state.remoteStreams.set(peerId, stream);
          this._emitState();
        },
      },
      false, // LAN STUN fallback flag — superseded if iceConfig is set
      this._iceConfig ?? undefined,
    );
  }

  private _createGroupManager(): void {
    if (this.groupManager && !this.groupManager.destroyed) {
      this.groupManager.destroy();
    }

    this.groupManager = new GroupCallManager({
      localUserId: this._state.localUserId,
      roomId: this._state.channelId || '',
      localStream: this._state.localStream,
      // Audit fix #8d: hand the server-issued ICE config (TURN creds)
      // down to every peer in the mesh. Without this every cross-
      // network group call silently fails at ICE.
      iceOverride: this._iceConfig ?? undefined,
      onSignal: (data) => {
        // sent_at_ms enables server-side stale-signal drop after
        // topology/route changes — see call_handlers.call_signal.
        socketManager.emitNoAck('call_signal', {
          call_id: this._state.callId,
          target_id: data.targetId,
          from_id: data.fromId,
          signal_type: data.type,
          sdp: data.sdp,
          candidate: data.candidate,
          sent_at_ms: Date.now(),
        });
      },
      onRemoteStream: (peerId, stream) => {
        this._state.remoteStreams.set(peerId, stream);

        // First remote stream → call is connected
        if (this.fsm.canTransition('CONNECTED')) {
          this._clearConnectTimer();
          this.fsm.transition('CONNECTED');
          this._state.startedAt = Date.now();
          this.qualityController.attachGroup(this.groupManager!);
          this.qualityController.start();
          // Setup reconnection manager for group calls
          this._setupReconnectionManager();
        }
        this._emitState();
      },
      onRemoteStreamRemoved: (peerId) => {
        this._state.remoteStreams.delete(peerId);
        this._emitState();
      },
      onParticipantJoined: (participant) => {
        this._state.participants.set(participant.peerId, participant);
        // Wire the per-peer reconnection manager. Previously the
        // GroupReconnectionManager was constructed in
        // _setupReconnectionManager but never actually received any
        // peers — `forPeer` was never called, so its quality
        // monitoring + proactive ICE restart logic was dead code.
        // Each PeerConnection has its own internal `_attemptIceRestart`
        // path, so this is additive recovery: state machine tracking +
        // network-change driven retries on top of the existing
        // failed-state restart.
        if (this.groupReconnectionManager && participant.connection) {
          const peerWrapper = participant.connection;
          const mgr = this.groupReconnectionManager.forPeer(
            participant.peerId,
            {
              // Delegate restart through the peer wrapper which knows
              // the signaling channel. Without this the manager's
              // built-in setLocalDescription path is silent.
              onIceRestartRequested: () => {
                if (peerWrapper && !peerWrapper.destroyed) {
                  (peerWrapper as any)._attemptIceRestart?.();
                }
              },
            },
          );
          mgr.attachPeerConnection(peerWrapper.peerConnection);
        }
        this.callbacks.onParticipantJoined(participant);
        this._emitState();
      },
      onParticipantLeft: (peerId) => {
        this._state.participants.delete(peerId);
        if (this.groupReconnectionManager) {
          this.groupReconnectionManager.removePeer(peerId);
        }
        this.callbacks.onParticipantLeft(peerId);
        this._emitState();
      },
      onPeerStateChange: (peerId, state) => {
        console.log(`[CallEngine] Peer ${peerId} state: ${state}`);
        if (this.groupReconnectionManager) {
          // Forward the per-peer connectionState so the manager's
          // state machine can transition through monitoring/recovering.
          // Without this, the manager has no way to know any peer ever
          // had trouble.
          this.groupReconnectionManager
            .forPeer(peerId)
            .onPeerConnectionStateChange(state);
        }
      },
      onMeshWarning: (message) => {
        console.warn(`[CallEngine] Mesh warning: ${message}`);
      },
    });

    // ── Hybrid topology coordinator ──
    // Listens for server-driven topology_switch events, renegotiates mesh,
    // or switches to SFU transport as instructed. Also publishes periodic
    // call_quality_report samples so the server's TopologyManager can decide.
    try {
      if (this.topologyCoordinator) {
        this.topologyCoordinator.destroy();
        this.topologyCoordinator = null;
      }
      if (this._state.callId && this.groupManager) {
        // Build SFU adapter with call context + a remote-stream hook so
        // incoming SFU consumers surface as remote streams in our state map.
        const sfuAdapter = new MediasoupSFUAdapter({
          callId: this._state.callId,
          localUserId: this._state.localUserId,
          onRemoteStream: (peerId: string, stream: MediaStream) => {
            this._state.remoteStreams.set(peerId, stream);
            this._emitState();
          },
          onRemoteStreamEnded: (peerId: string) => {
            // Only drop if we no longer have any tracks for this peer in
            // either mesh or SFU maps; CallEngine tracks remoteStreams as
            // single entries, so safest is to let the next produce refresh
            // the entry.
            const existing = this._state.remoteStreams.get(peerId);
            if (existing && existing.getTracks().length === 0) {
              this._state.remoteStreams.delete(peerId);
              this._emitState();
            }
          },
        });
        this.topologyCoordinator = new TopologyCoordinator({
          callId: this._state.callId,
          localUserId: this._state.localUserId,
          groupManager: this.groupManager,
          sfuAdapter,
          onRoutingChanged: (routing: CallRoutingMode, gen: number) => {
            // Client-side `routing` uses 'p2p' | 'mesh'; SFU rides on top of mesh
            // from the app's state machine perspective (participants still rendered
            // via GroupCallManager). Preserve 'mesh' for anything non-p2p.
            this._state.routing = routing === 'p2p' ? 'p2p' : 'mesh';
            console.log(`[CallEngine] topology → ${routing} (gen=${gen})`);
            this._emitState();
          },
          onError: (msg: string) => {
            console.warn('[CallEngine] topology error:', msg);
          },
        });
        this.topologyCoordinator.start();
      }
    } catch (err) {
      console.error('[CallEngine] topology coordinator init failed:', err);
    }

    // ── Call heartbeat ──
    // Server's orphan-sweeper reaps calls whose participants haven't reported
    // in 90s. Emit a heartbeat every 20s while we're in an active group call.
    this._startCallHeartbeat();
  }

  // ── Private: Call Heartbeat ───────────────────────

  private _startCallHeartbeat(): void {
    this._stopCallHeartbeat();
    if (!this._state.callId) return;

    const emit = () => {
      try {
        if (!this._state.callId) return;
        // Fire-and-forget. Server handler: on('call_heartbeat') updates
        // ActiveCallParticipant.last_heartbeat_at.
        socketManager.emitNoAck('call_heartbeat', {
          call_id: this._state.callId,
          user_id: this._state.localUserId,
          ts: Date.now(),
        });
      } catch (err) {
        console.warn('[CallEngine] heartbeat emit failed:', err);
      }
    };

    // Emit one immediately so the server marks us alive right away.
    emit();
    this._callHeartbeatTimer = setInterval(emit, CallEngine.CALL_HEARTBEAT_INTERVAL_MS);
  }

  private _stopCallHeartbeat(): void {
    if (this._callHeartbeatTimer) {
      clearInterval(this._callHeartbeatTimer);
      this._callHeartbeatTimer = null;
    }
  }

  // ── Private: Socket Listeners ─────────────────────

  private _registerSocketListeners(): void {
    // Incoming call
    this._socketUnsubs.push(
      socketManager.on('call_incoming', (data: any) => {
        if (this.fsm.state !== 'idle') {
          // Busy — auto-reject
          socketManager.emitNoAck('v2_call_reject', {
            call_id: data.call_id,
            caller_id: data.caller_id,
          });
          return;
        }

        this._state.callId = data.call_id;
        this._state.remoteUserId = data.caller_id;
        this._state.type = data.media_type || 'audio';
        this._state.routing = data.channel_id ? 'mesh' : 'p2p';
        this._state.channelId = data.channel_id || null;
        this._state.isInitiator = false;

        this.fsm.transition('INCOMING');

        this.callbacks.onIncomingCall({
          callId: data.call_id,
          callerId: data.caller_id,
          callerName: data.caller_name || 'Unknown',
          mediaType: data.media_type || 'audio',
          channelId: data.channel_id,
        });

        // Ring timeout (incoming variant — separate slot from outgoing)
        this._clearIncomingRingTimer();
        this._incomingRingTimer = setTimeout(() => {
          if (this.fsm.state === 'ringing') {
            this.fsm.transition('TIMEOUT');
          }
        }, RING_TIMEOUT_MS);
      })
    );

    // Call accepted by remote (for initiator side)
    this._socketUnsubs.push(
      socketManager.on('call_accepted', (data: any) => {
        if (this._state.callId !== data.call_id) return;
        this._clearRingTimer();

        if (this.fsm.canTransition('PEER_READY')) {
          this.fsm.transition('PEER_READY');
        }

        // Create peer connection — initiator creates the offer.
        // Fire-and-forget the await: the listener can't be async and
        // we don't want to block the socket-event loop. The PC is
        // ready before any ICE candidate arrives because handleSignal
        // auto-adds peers + buffers ICE.
        if (this._state.routing === 'p2p' && this._state.remoteUserId) {
          void this._createPeerConnection(this._state.remoteUserId, true);
          this._startConnectTimer();
        }
      })
    );

    // Call rejected by remote
    this._socketUnsubs.push(
      socketManager.on('call_rejected', (data: any) => {
        if (this._state.callId !== data.call_id) return;
        this.fsm.transition('REJECT');
      })
    );

    // Remote hangup
    this._socketUnsubs.push(
      socketManager.on('call_hangup', (data: any) => {
        if (this._state.callId !== data.call_id) return;
        this.fsm.transition('REMOTE_HANGUP');
      })
    );

    // Signaling relay (offer, answer, ICE candidate)
    this._socketUnsubs.push(
      socketManager.on('call_signal', (data: any) => {
        if (this._state.callId !== data.call_id) return;

        const signal: SignalMessage = {
          type: data.signal_type,
          targetId: this._state.localUserId,
          fromId: data.from_id,
          sdp: data.sdp,
          candidate: data.candidate,
        };

        if (this._state.routing === 'p2p' && this.peerConnection) {
          this._handleP2PSignal(signal);
        } else if (this._state.routing === 'mesh' && this.groupManager) {
          this.groupManager.handleSignal(data.from_id, signal);
        }
      })
    );

    // Group: participant joined
    this._socketUnsubs.push(
      socketManager.on('call_participant_joined', (data: any) => {
        if (this._state.callId !== data.call_id) return;
        if (!this.groupManager || data.user_id === this._state.localUserId) return;

        this.groupManager.addParticipant(
          data.user_id,
          this._state.localUserId < data.user_id
        );
      })
    );

    // Group: participant left
    this._socketUnsubs.push(
      socketManager.on('call_participant_left', (data: any) => {
        if (this._state.callId !== data.call_id) return;
        if (!this.groupManager) return;

        this.groupManager.removeParticipant(data.user_id);

        // If no participants left, end the call
        if (this.groupManager.participantCount === 0) {
          this.fsm.transition('REMOTE_HANGUP');
        }
      })
    );

    // Remote mute/video toggle
    this._socketUnsubs.push(
      socketManager.on('call_participant_state', (data: any) => {
        if (this._state.callId !== data.call_id) return;

        if (this.groupManager) {
          this.groupManager.updateParticipantState(data.user_id, {
            isAudioMuted: data.muted,
            isVideoOff: data.video_off,
            isSharingScreen: data.sharing_screen,
          });
        }

        this._emitState();
      })
    );

    // Hand-raise toggle broadcast — webinar UX. Without listening,
    // remote raises don't show on tiles and the host has no signal
    // to grant the floor.
    this._socketUnsubs.push(
      socketManager.on('call:hand-changed', (data: any) => {
        if (this._state.callId !== data?.call_id) return;
        const userId = data?.user_id;
        if (typeof userId !== 'string') return;
        if (this.groupManager) {
          this.groupManager.updateParticipantState(userId, {
            isHandRaised: !!data.raised,
            handRaisedAt: data.raised_at ?? null,
          });
        }
        if (userId === this._state.localUserId) {
          this._state.isHandRaised = !!data.raised;
        }
        this._emitState();
      })
    );

    // Live in-call reaction (emoji float-up). Just hand off to the
    // optional callback — the store renders the floating emoji for
    // ~2 seconds and clears it. No state needs to live in the engine.
    this._socketUnsubs.push(
      socketManager.on('call:reaction', (data: any) => {
        if (this._state.callId !== data?.call_id) return;
        try {
          this.callbacks.onReaction?.({
            callId: data.call_id,
            userId: data.user_id,
            emoji: data.emoji,
            ts: data.ts || Date.now(),
          });
        } catch { /* ignore optional callback errors */ }
      })
    );

    // Live caption from whisper-cli. The store appends the line to
    // a rolling buffer; the captions overlay renders the most recent
    // few lines per speaker.
    this._socketUnsubs.push(
      socketManager.on('call:caption', (data: any) => {
        if (this._state.callId !== data?.call_id) return;
        try {
          this.callbacks.onCaption?.({
            callId: data.call_id,
            userId: data.user_id,
            text: data.text || '',
            language: data.language,
            ts: data.ts || Date.now(),
          });
        } catch { /* ignore */ }
      })
    );

    // Call error from server
    this._socketUnsubs.push(
      socketManager.on('call_error', (data: any) => {
        console.error('[CallEngine] Server call error:', data);
        this.callbacks.onError(data.message || 'Call error');
        if (this.fsm.isLive) {
          this.fsm.transition('ERROR');
        }
      })
    );

    // Host promotion — server picks a new host when the original
    // initiator leaves a group call. Without listening, the UI keeps
    // showing the old host's controls (kick/end-for-all) on a user
    // who's no longer there. Update local state so the host menu
    // re-renders for the newly-promoted participant.
    this._socketUnsubs.push(
      socketManager.on('call:host-changed', (data: any) => {
        if (this._state.callId !== data?.call_id) return;
        const newHost = data?.new_host;
        if (typeof newHost !== 'string') return;
        // Mutate state and re-emit so subscribers (UI / store) pick up
        // the new host id. Keeping this minimal: callers read
        // hostId via _state, no separate setter needed.
        (this._state as any).hostId = newHost;
        try {
          this.callbacks.onHostChanged?.({
            callId: data.call_id,
            oldHost: data.old_host,
            newHost,
          });
        } catch { /* ignore optional callback errors */ }
        this._emitState();
      })
    );

    // Force-mute / kick from a moderator — purely informational; the
    // server already mutated authoritative state. We display a toast
    // so the user knows it wasn't a UI glitch.
    this._socketUnsubs.push(
      socketManager.on('call:force_muted', (data: any) => {
        if (this._state.callId !== data?.call_id) return;
        try {
          this.callbacks.onModerationEvent?.({
            type: 'force_muted',
            callId: data.call_id,
            muted: !!data.muted,
            byUserId: data.by,
          });
        } catch { /* ignore */ }
      })
    );
    this._socketUnsubs.push(
      socketManager.on('call:kicked', (data: any) => {
        if (this._state.callId !== data?.call_id) return;
        try {
          this.callbacks.onModerationEvent?.({
            type: 'kicked',
            callId: data.call_id,
            reason: data.reason,
            byUserId: data.by,
          });
        } catch { /* ignore */ }
        // Tear down the local call — server already evicted us.
        if (this.fsm.isLive) {
          this.fsm.transition('ERROR');
        }
      })
    );
  }

  private _unregisterSocketListeners(): void {
    for (const unsub of this._socketUnsubs) {
      unsub();
    }
    this._socketUnsubs = [];
  }

  private async _handleP2PSignal(signal: SignalMessage): Promise<void> {
    if (!this.peerConnection) return;

    switch (signal.type) {
      case 'offer':
      case 'renegotiate':
        if (signal.sdp) await this.peerConnection.handleOffer(signal.sdp);
        break;
      case 'answer':
        if (signal.sdp) await this.peerConnection.handleAnswer(signal.sdp);
        break;
      case 'ice-candidate':
        if (signal.candidate) await this.peerConnection.handleIceCandidate(signal.candidate);
        break;
    }
  }

  // ── Private: Timers ───────────────────────────────

  private _startConnectTimer(): void {
    this._clearConnectTimer();
    this._connectTimer = setTimeout(() => {
      if (this.fsm.state === 'connecting') {
        console.warn('[CallEngine] Connect timeout');
        this.fsm.transition('TIMEOUT');
      }
    }, CONNECT_TIMEOUT_MS);
  }

  private _clearConnectTimer(): void {
    if (this._connectTimer) {
      clearTimeout(this._connectTimer);
      this._connectTimer = null;
    }
  }

  private _clearRingTimer(): void {
    this._clearOutgoingRingTimer();
    this._clearIncomingRingTimer();
  }

  private _clearOutgoingRingTimer(): void {
    if (this._outgoingRingTimer) {
      clearTimeout(this._outgoingRingTimer);
      this._outgoingRingTimer = null;
    }
  }

  private _clearIncomingRingTimer(): void {
    if (this._incomingRingTimer) {
      clearTimeout(this._incomingRingTimer);
      this._incomingRingTimer = null;
    }
  }

  private _startReconnectTimer(): void {
    this._clearReconnectTimer();
    this._reconnectTimer = setTimeout(() => {
      if (this.fsm.state === 'reconnecting') {
        console.warn('[CallEngine] Reconnect timeout — ending call');
        this.callbacks.onError('Connection lost — reconnect timed out');
        this.fsm.transition('RECONNECT_FAILED');
      }
    }, RECONNECT_TIMEOUT_MS);
  }

  private _clearReconnectTimer(): void {
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }

  // ── Private: State Emission ───────────────────────

  private _emitState(): void {
    this.callbacks.onStateChange({ ...this._state });
  }

  // ── Private: Cleanup ──────────────────────────────

  private _cleanup(reason: string): void {
    this._clearRingTimer();
    this._clearConnectTimer();
    this._clearReconnectTimer();

    // Stop call heartbeat
    this._stopCallHeartbeat();

    // Stop quality monitoring
    this.qualityController.stop();

    // Tear down topology coordinator (stops quality reports + unsubs topology_switch)
    if (this.topologyCoordinator) {
      try {
        this.topologyCoordinator.destroy();
      } catch (err) {
        console.error('[CallEngine] topology coordinator destroy error:', err);
      }
      this.topologyCoordinator = null;
    }

    // Destroy peer connections
    if (this.peerConnection && !this.peerConnection.destroyed) {
      this.peerConnection.destroy();
      this.peerConnection = null;
    }

    if (this.groupManager && !this.groupManager.destroyed) {
      this.groupManager.destroy();
      this.groupManager = null;
    }

    // Stop recording if active
    if (this._mediaRecorder && this._mediaRecorder.state === 'recording') {
      this._mediaRecorder.stop();
    }
    this._recordingChunks = [];
    this._mediaRecorder = null;

    // Destroy reconnection managers
    if (this.reconnectionManager) {
      this.reconnectionManager.destroy();
      this.reconnectionManager = null;
    }

    if (this.groupReconnectionManager) {
      this.groupReconnectionManager.destroy();
      this.groupReconnectionManager = null;
    }

    // Release media
    this.deviceManager.releaseLocalStream();
    this.deviceManager.releaseScreenStream();

    // Reset screen senders and participant volumes
    this.screenSenders.clear();
    this._participantVolumes.clear();

    // Reset state
    const userId = this._state.localUserId;
    this._state = {
      callId: null,
      status: 'idle',
      type: 'audio',
      routing: 'p2p',
      isInitiator: false,
      localUserId: userId,
      remoteUserId: null,
      channelId: null,
      localStream: null,
      remoteStreams: new Map(),
      participants: new Map(),
      isMuted: false,
      isVideoOff: false,
      isScreenSharing: false,
      screenStream: null,
      qualityLevel: 'excellent',
      startedAt: null,
      error: null,
      isOnHold: false,
      isHandRaised: false,
    };

    this._isOnHold = false;
    this._autoQualityEnabled = false;

    this.fsm.reset();

    this.callbacks.onCallEnded(reason);
    console.log(`[CallEngine] Cleanup complete (reason: ${reason})`);
  }
}
