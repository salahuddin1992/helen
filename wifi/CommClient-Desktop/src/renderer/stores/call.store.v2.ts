/**
 * call.store.v2.ts — Zustand store powered by the new CallEngine.
 *
 * Drop-in replacement for call.store.ts.
 * Uses CallEngine for all call lifecycle, signaling, media, and quality control.
 *
 * To migrate: replace `import { useCallStore } from './call.store'`
 * with `import { useCallStore } from './call.store.v2'` in App.tsx and components.
 */

import { create } from 'zustand';
import {
  CallEngine,
  CallEngineState,
  CallType,
  QualityLevel,
} from '../services/call';
import type { GroupParticipant, QualityChangeEvent, DeviceInfo } from '../services/call';
import type { QualityController } from '../services/call/QualityController';
import type { IncomingCall } from '../types';

// ── Store Types ─────────────────────────────────────

interface CallStoreState {
  // Engine state (mirrors CallEngineState)
  callId: string | null;
  status: 'idle' | 'ringing' | 'connecting' | 'active' | 'reconnecting' | 'ended';
  type: CallType;
  routing: 'p2p' | 'mesh';
  isInitiator: boolean;
  remoteUserId: string | null;
  /** user_id of the current call host (initiator or post-promotion). */
  hostId: string | null;
  channelId: string | null;
  localStream: MediaStream | null;
  remoteStreams: Record<string, MediaStream>;
  participants: Record<string, GroupParticipant>;
  isMuted: boolean;
  isVideoOff: boolean;
  isHandRaised: boolean;
  isScreenSharing: boolean;
  screenStream: MediaStream | null;
  qualityLevel: QualityLevel;
  startedAt: number | null;
  error: string | null;
  isOnHold: boolean;

  // Push-to-talk & noise suppression
  isPushToTalk: boolean;
  isNoiseSuppression: boolean;

  // UI state
  incomingCall: IncomingCall | null;
  callDuration: number;
  durationInterval: ReturnType<typeof setInterval> | null;
  devices: DeviceInfo[];

  /** Active floating reactions: emoji + sender id, keyed by a unique
   *  id so React can animate enter/exit. Auto-removed after ~2s. */
  activeReactions: Array<{ id: string; userId: string; emoji: string; ts: number }>;
  /** Reason the call ended, displayed once on the post-call screen.
   *  Cleared when a new call is initiated. */
  endReason: string | null;
  /** Per-peer volume gain (0-1.5, where 1 = normal). */
  peerVolumes: Record<string, number>;

  // Actions
  initEngine: (localUserId: string) => void;
  destroyEngine: () => void;
  initiateCall: (targetUserId: string, mediaType: CallType) => Promise<void>;
  initiateGroupCall: (channelId: string, mediaType: CallType) => Promise<void>;
  acceptCall: () => Promise<void>;
  rejectCall: () => void;
  hangup: () => void;
  toggleMute: () => void;
  toggleVideo: () => void;
  toggleHand: () => void;
  sendReaction: (emoji: string) => void;
  setPeerVolume: (peerId: string, volume: number) => void;
  clearEndReason: () => void;
  videoEffect: 'none' | 'blur' | 'darken' | 'image';
  setVideoEffect: (effect: 'none' | 'blur' | 'darken' | 'image') => Promise<void>;
  /** Set or clear a custom virtual-background image. */
  setVideoBackgroundImage: (src: string) => Promise<void>;
  liveCaptionsEnabled: boolean;
  toggleLiveCaptions: () => void;
  /** Rolling caption buffer — last N lines, each tagged with speaker. */
  captions: Array<{ id: string; userId: string; text: string; ts: number }>;
  clearCaptions: () => void;
  /** Co-host user IDs — server-authoritative, mirrored from
   *  ``call:cohost_changed`` socket events. Co-hosts share moderation
   *  privileges with the host. */
  coHostIds: string[];

  /** Pending pre-join intent. Renderer mounts <PreJoinScreen /> when
   *  this is set and the screen calls clearPreJoin() on confirm/cancel. */
  preJoinIntent:
    | { kind: 'accept'; title?: string }
    | { kind: 'initiate-1to1'; targetId: string; type: 'audio' | 'video'; title?: string }
    | { kind: 'initiate-group'; channelId: string; type: 'audio' | 'video'; title?: string }
    | null;
  showPreJoin: (intent: NonNullable<CallStoreState['preJoinIntent']>) => void;
  clearPreJoin: () => void;
  startScreenShare: (sourceId: string) => Promise<void>;
  stopScreenShare: () => void;
  pauseScreenShare: () => void;
  resumeScreenShare: () => void;
  switchScreenSource: (sourceId: string) => Promise<void>;
  isScreenPaused: boolean;
  switchAudioInput: (deviceId: string) => Promise<void>;
  switchVideoInput: (deviceId: string) => Promise<void>;
  holdCall: () => void;
  resumeCall: () => void;
  setQualityPreset: (preset: string) => Promise<void>;
  refreshDevices: () => Promise<void>;
  togglePushToTalk: () => void;
  toggleNoiseSuppression: () => void;
  pushToTalkKeyDown: () => void;
  pushToTalkKeyUp: () => void;

  // Media policy / quality controller access
  getQualityController: () => QualityController | null;
  applyServerMediaCap: (
    cap: {
      max_width: number;
      max_height: number;
      max_framerate: number;
      max_bitrate_kbps: number;
      allow_8k: boolean;
      allow_client_override: boolean;
      enforce_hard_cap: boolean;
    },
    ladder?: Array<{ id: string }>,
  ) => void;
}

// ── Engine Singleton ────────────────────────────────

let engine: CallEngine | null = null;
let _initializing = false;

// Noise suppression Web Audio nodes (kept outside store for cleanup)
let _noiseSuppressionCtx: AudioContext | null = null;
let _noiseSuppressionSource: MediaStreamAudioSourceNode | null = null;
let _noiseSuppressionDest: MediaStreamAudioDestinationNode | null = null;
let _originalAudioTrack: MediaStreamTrack | null = null;

// ── Store ───────────────────────────────────────────

export const useCallStore = create<CallStoreState>((set, get) => ({
  // Initial state
  callId: null,
  status: 'idle',
  type: 'audio',
  routing: 'p2p',
  isInitiator: false,
  remoteUserId: null,
  hostId: null,
  channelId: null,
  localStream: null,
  remoteStreams: {},
  participants: {},
  isMuted: false,
  isVideoOff: false,
  isHandRaised: false,
  isScreenSharing: false,
  screenStream: null,
  qualityLevel: 'excellent',
  startedAt: null,
  error: null,
  isOnHold: false,
  isPushToTalk: false,
  isNoiseSuppression: false,
  isScreenPaused: false,
  incomingCall: null,
  callDuration: 0,
  durationInterval: null,
  devices: [],
  activeReactions: [],
  endReason: null,
  peerVolumes: {},
  videoEffect: 'none',
  liveCaptionsEnabled: false,
  captions: [],
  preJoinIntent: null,
  coHostIds: [],

  // ── Lifecycle ──────────────────────────────────────

  initEngine: (localUserId: string) => {
    if (_initializing) return;
    _initializing = true;

    if (engine) {
      engine.destroy();
    }

    engine = new CallEngine(localUserId, {
      onStateChange: (state: CallEngineState) => {
        // Convert Map to Record for Zustand compatibility
        const remoteStreams: Record<string, MediaStream> = {};
        state.remoteStreams.forEach((stream, peerId) => {
          remoteStreams[peerId] = stream;
        });

        const participants: Record<string, GroupParticipant> = {};
        state.participants.forEach((p, peerId) => {
          participants[peerId] = p;
        });

        set({
          callId: state.callId,
          status: state.status,
          type: state.type,
          routing: state.routing,
          isInitiator: state.isInitiator,
          remoteUserId: state.remoteUserId,
          // hostId is mirrored from engine state — set by host-changed
          // listener and on initial join. isInitiator stays for the
          // ORIGINAL initiator; hostId follows the active host.
          hostId: (state as any).hostId ?? state.remoteUserId ?? null,
          channelId: state.channelId,
          localStream: state.localStream,
          remoteStreams,
          participants,
          isMuted: state.isMuted,
          isVideoOff: state.isVideoOff,
          isHandRaised: state.isHandRaised ?? false,
          isScreenSharing: state.isScreenSharing,
          screenStream: state.screenStream,
          qualityLevel: state.qualityLevel,
          startedAt: state.startedAt,
          error: state.error,
          isOnHold: state.isOnHold,
        });

        // Start/stop duration timer
        if (state.status === 'active' && !get().durationInterval) {
          const interval = setInterval(() => {
            const { startedAt } = get();
            if (startedAt) {
              set({ callDuration: Math.floor((Date.now() - startedAt) / 1000) });
            }
          }, 1000);
          set({ durationInterval: interval });
        }

        if (state.status === 'idle' || state.status === 'ended') {
          const { durationInterval } = get();
          if (durationInterval) {
            clearInterval(durationInterval);
            set({ durationInterval: null, callDuration: 0 });
          }
        }
      },

      onIncomingCall: (data) => {
        const incoming: IncomingCall = {
          call_id: data.callId,
          caller_id: data.callerId,
          caller_name: data.callerName,
          media_type: data.mediaType,
          channel_id: data.channelId,
        };
        set({ incomingCall: incoming });

        // Native notification + force-window-front. Incoming calls
        // need the strongest possible attention path: notification AND
        // bring-to-foreground so the user can accept/decline before
        // the caller gives up.
        window.electronAPI?.showNotification(
          'Incoming Call',
          `${data.callerName} is calling...`
        );
        window.electronAPI?.forceFocusWindow?.();

        // Android-native heads-up notification with Accept / Decline
        // actions, full-screen intent so it wakes the screen even from
        // a locked device. No-op on desktop / web.
        try {
          window.electronAPI?.call?.notifyIncoming?.({
            callerName: data.callerName,
            callerId:   data.callerId,
            channelId:  data.channelId ?? data.callId,
            isVideo:    data.mediaType === 'video',
          });
        } catch { /* best-effort */ }
      },

      onCallEnded: (reason) => {
        const { durationInterval } = get();
        if (durationInterval) {
          clearInterval(durationInterval);
        }
        // Map raw reason to a human-readable label that the
        // post-call screen can show. The reason is one of:
        // 'hangup' | 'remote_hangup' | 'reject' | 'timeout' |
        // 'host_left' | 'last_participant' | 'network_drop' |
        // 'error' | 'idle'. Unknown reasons fall back to a
        // generic message rather than leaking the raw token.
        const labels: Record<string, string> = {
          hangup: 'انتهت المكالمة',
          remote_hangup: 'الطرف الآخر أنهى المكالمة',
          reject: 'تم رفض المكالمة',
          timeout: 'انتهت المهلة بدون رد',
          host_left: 'المضيف غادر المكالمة',
          last_participant: 'لم يبق مشاركون',
          network_drop: 'انقطع الاتصال بالشبكة',
          error: 'حدث خطأ في المكالمة',
        };
        const endReason = labels[reason] || 'انتهت المكالمة';
        set({
          incomingCall: null,
          callDuration: 0,
          durationInterval: null,
          activeReactions: [],
          peerVolumes: {},
          endReason,
        });
        // Dismiss any Android heads-up notification that may still be up
        // (e.g. user accepted via in-app sheet, not via notification action).
        try { window.electronAPI?.call?.cancelIncoming?.(); } catch { /* ignore */ }
        console.log(`[CallStore] Call ended: ${reason}`);
      },

      onCaption: ({ userId, text, ts }) => {
        const id = `${ts}-${userId}-${Math.random().toString(36).slice(2, 6)}`;
        const cur = get().captions;
        // Cap the rolling buffer at 10 lines so the UI doesn't
        // accumulate hours of meeting transcript in memory; long-
        // term archival is the call recording's job.
        const next = [...cur.slice(-9), { id, userId, text, ts }];
        set({ captions: next });
      },

      onReaction: ({ userId, emoji, ts }) => {
        // Append to the floating list. UI auto-removes the entry
        // after ~2s; we also self-prune to keep the array bounded
        // in case a flood comes in (15 reactions/sec from one user
        // is enough to fill the screen — the UI cap is enough but
        // capping the array too keeps GC happy).
        const id = `${ts}-${userId}-${Math.random().toString(36).slice(2, 6)}`;
        const cur = get().activeReactions;
        const next = [...cur.slice(-19), { id, userId, emoji, ts }];
        set({ activeReactions: next });
        setTimeout(() => {
          const after = get().activeReactions.filter((r) => r.id !== id);
          set({ activeReactions: after });
        }, 2200);
      },

      onError: (error) => {
        set({ error });
        console.error(`[CallStore] Error: ${error}`);
      },

      // Host promotion — server picked a new host (the original
      // initiator left). Mirror into store so HostMenu re-renders.
      onHostChanged: ({ newHost }) => {
        set({ hostId: newHost });
        console.log(`[CallStore] host changed → ${newHost}`);
      },

      // Moderator action targeting THIS user. Surface as a toast-like
      // error message; the engine already adjusted local state if a
      // hard action was applied (force-mute / kick).
      onModerationEvent: (data) => {
        if (data.type === 'kicked') {
          set({ error: `Removed from call: ${data.reason || 'kicked by moderator'}` });
        } else if (data.type === 'force_muted') {
          set({ isMuted: !!data.muted });
        }
      },

      onParticipantJoined: (_participant) => {
        // State already updated via onStateChange
      },

      onParticipantLeft: (_peerId) => {
        // State already updated via onStateChange
      },

      onQualityChange: (_event: QualityChangeEvent) => {
        // Quality level already updated via onStateChange
      },
    });

    // Initialize engine (enumerate devices, register socket listeners)
    engine.init().then(() => {
      // Refresh device list
      get().refreshDevices();
      _initializing = false;
    }).catch(() => {
      _initializing = false;
    });
  },

  destroyEngine: () => {
    const { durationInterval } = get();
    if (durationInterval) clearInterval(durationInterval);

    if (engine) {
      engine.destroy();
      engine = null;
    }

    set({
      callId: null,
      status: 'idle',
      type: 'audio',
      routing: 'p2p',
      isInitiator: false,
      remoteUserId: null,
      channelId: null,
      localStream: null,
      remoteStreams: {},
      participants: {},
      isMuted: false,
      isVideoOff: false,
      isScreenSharing: false,
      screenStream: null,
      qualityLevel: 'excellent',
      startedAt: null,
      error: null,
      isOnHold: false,
      isPushToTalk: false,
      isNoiseSuppression: false,
      incomingCall: null,
      callDuration: 0,
      durationInterval: null,
    });

    // Clean up noise suppression
    if (_noiseSuppressionCtx) {
      _noiseSuppressionCtx.close().catch(() => {});
      _noiseSuppressionCtx = null;
      _noiseSuppressionSource = null;
      _noiseSuppressionDest = null;
      _originalAudioTrack = null;
    }
  },

  // ── Call Actions ───────────────────────────────────

  initiateCall: async (targetUserId, mediaType) => {
    if (!engine) return;
    set({ endReason: null });
    await engine.initiateCall(targetUserId, mediaType);
  },

  initiateGroupCall: async (channelId, mediaType) => {
    if (!engine) return;
    set({ endReason: null });
    await engine.initiateGroupCall(channelId, mediaType);
  },

  acceptCall: async () => {
    if (!engine) return;
    set({ incomingCall: null, endReason: null });
    await engine.acceptCall();
  },

  rejectCall: () => {
    if (!engine) return;
    set({ incomingCall: null });
    engine.rejectCall();
  },

  hangup: () => {
    if (!engine) return;
    engine.hangup();
  },

  holdCall: () => {
    if (!engine) return;
    engine.holdCall();
    set({ isOnHold: true });
  },

  resumeCall: () => {
    if (!engine) return;
    engine.resumeCall();
    set({ isOnHold: false });
  },

  // ── Media Controls ────────────────────────────────

  toggleMute: () => {
    if (!engine) return;
    engine.toggleMute();
  },

  toggleVideo: () => {
    if (!engine) return;
    engine.toggleVideo();
  },

  toggleHand: () => {
    if (!engine) return;
    engine.toggleHand();
  },

  sendReaction: (emoji) => {
    if (!engine) return;
    engine.sendReaction(emoji);
  },

  setPeerVolume: (peerId, volume) => {
    const clamped = Math.max(0, Math.min(1.5, volume));
    set((s) => ({ peerVolumes: { ...s.peerVolumes, [peerId]: clamped } }));
  },

  clearEndReason: () => set({ endReason: null }),

  setVideoEffect: async (effect) => {
    if (!engine) return;
    await engine.setVideoEffect(effect);
    set({ videoEffect: effect });
  },

  setVideoBackgroundImage: async (src) => {
    if (!engine) return;
    await engine.setVideoBackgroundImage(src);
  },

  toggleLiveCaptions: () => {
    if (!engine) return;
    const next = !get().liveCaptionsEnabled;
    engine.setLiveCaptions(next);
    set({ liveCaptionsEnabled: next });
    if (!next) set({ captions: [] });
  },

  clearCaptions: () => set({ captions: [] }),

  showPreJoin: (intent) => set({ preJoinIntent: intent }),
  clearPreJoin: () => set({ preJoinIntent: null }),

  startScreenShare: async (sourceId) => {
    if (!engine) return;
    await engine.startScreenShare(sourceId);
  },

  stopScreenShare: () => {
    if (!engine) return;
    engine.stopScreenShare();
    set({ isScreenPaused: false });
  },

  pauseScreenShare: () => {
    const { screenStream, isScreenSharing } = get();
    if (!isScreenSharing || !screenStream) return;
    // Disable the video track — peers see a black/frozen frame, connection persists.
    // This is the standard WebRTC pause technique.
    for (const track of screenStream.getVideoTracks()) {
      track.enabled = false;
    }
    set({ isScreenPaused: true });
  },

  resumeScreenShare: () => {
    const { screenStream, isScreenSharing } = get();
    if (!isScreenSharing || !screenStream) return;
    for (const track of screenStream.getVideoTracks()) {
      track.enabled = true;
    }
    set({ isScreenPaused: false });
  },

  switchScreenSource: async (sourceId: string) => {
    if (!engine) return;
    // Stop current share, then start with new source.
    // The CallEngine handles peer track replacement internally.
    try {
      engine.stopScreenShare();
      await engine.startScreenShare(sourceId);
      set({ isScreenPaused: false });
    } catch (e: any) {
      console.error('[call.store] switchScreenSource failed:', e);
      set({ error: e?.message || 'Failed to switch screen source' });
    }
  },

  switchAudioInput: async (deviceId) => {
    if (!engine) return;
    await engine.switchAudioInput(deviceId);
  },

  switchVideoInput: async (deviceId) => {
    if (!engine) return;
    await engine.switchVideoInput(deviceId);
  },

  setQualityPreset: async (preset) => {
    if (!engine) return;
    await engine.setQualityPreset(preset);
  },

  refreshDevices: async () => {
    if (!engine) return;
    const devices = await engine.mediaDevices.enumerateDevices();
    set({ devices });
  },

  // ── Push-to-Talk ─────────────────────────────────

  togglePushToTalk: () => {
    const { isPushToTalk } = get();
    const enabling = !isPushToTalk;
    set({ isPushToTalk: enabling });

    if (enabling && engine) {
      // When PTT is enabled, mute by default
      const { isMuted } = get();
      if (!isMuted) {
        engine.toggleMute();
      }
    } else if (!enabling && engine) {
      // When PTT is disabled, unmute if currently muted
      const { isMuted } = get();
      if (isMuted) {
        engine.toggleMute();
      }
    }
  },

  pushToTalkKeyDown: () => {
    const { isPushToTalk, isMuted } = get();
    if (!isPushToTalk || !engine) return;
    // Temporarily unmute while key is held
    if (isMuted) {
      engine.toggleMute();
    }
  },

  pushToTalkKeyUp: () => {
    const { isPushToTalk, isMuted } = get();
    if (!isPushToTalk || !engine) return;
    // Re-mute when key is released
    if (!isMuted) {
      engine.toggleMute();
    }
  },

  // ── Noise Suppression ────────────────────────────

  toggleNoiseSuppression: () => {
    const { isNoiseSuppression, localStream } = get();
    const enabling = !isNoiseSuppression;
    set({ isNoiseSuppression: enabling });

    if (!localStream) return;

    if (enabling) {
      try {
        const audioTrack = localStream.getAudioTracks()[0];
        if (!audioTrack) return;

        _originalAudioTrack = audioTrack;

        const ctx = new AudioContext();
        _noiseSuppressionCtx = ctx;

        const source = ctx.createMediaStreamSource(new MediaStream([audioTrack]));
        _noiseSuppressionSource = source;

        // High-pass filter to remove low-frequency noise (rumble, hum)
        const highpass = ctx.createBiquadFilter();
        highpass.type = 'highpass';
        highpass.frequency.value = 85;
        highpass.Q.value = 0.7;

        // Second high-pass for steeper rolloff
        const highpass2 = ctx.createBiquadFilter();
        highpass2.type = 'highpass';
        highpass2.frequency.value = 120;
        highpass2.Q.value = 0.5;

        // Low-pass to cut high-frequency hiss
        const lowpass = ctx.createBiquadFilter();
        lowpass.type = 'lowpass';
        lowpass.frequency.value = 14000;
        lowpass.Q.value = 0.5;

        // Compressor to even out levels and reduce noise floor
        const compressor = ctx.createDynamicsCompressor();
        compressor.threshold.value = -30;
        compressor.knee.value = 12;
        compressor.ratio.value = 4;
        compressor.attack.value = 0.003;
        compressor.release.value = 0.15;

        const dest = ctx.createMediaStreamDestination();
        _noiseSuppressionDest = dest;

        source.connect(highpass);
        highpass.connect(highpass2);
        highpass2.connect(lowpass);
        lowpass.connect(compressor);
        compressor.connect(dest);

        // Replace audio track in the local stream
        const processedTrack = dest.stream.getAudioTracks()[0];
        localStream.removeTrack(audioTrack);
        localStream.addTrack(processedTrack);

        // Audit fix: also replace the track on every active
        // RTCRtpSender so the REMOTE peer hears the processed audio.
        // Without this, swapping the track on `localStream` only
        // affects the local <audio>/preview — the wire still carries
        // the original microphone signal.
        try {
          if (engine) (engine as any).replaceLocalAudioTrack?.(processedTrack);
        } catch (e) {
          console.warn('[CallStore] replaceLocalAudioTrack failed:', e);
        }
      } catch (err) {
        console.error('[CallStore] Failed to enable noise suppression:', err);
        set({ isNoiseSuppression: false });
      }
    } else {
      // Restore original audio track
      if (_originalAudioTrack && localStream) {
        const currentProcessed = localStream.getAudioTracks()[0];
        if (currentProcessed) {
          localStream.removeTrack(currentProcessed);
        }
        localStream.addTrack(_originalAudioTrack);
        // Audit fix: also restore the original track on every Sender.
        try {
          if (engine) (engine as any).replaceLocalAudioTrack?.(_originalAudioTrack);
        } catch (e) {
          console.warn('[CallStore] restore replaceLocalAudioTrack failed:', e);
        }
        _originalAudioTrack = null;
      }

      if (_noiseSuppressionCtx) {
        _noiseSuppressionCtx.close().catch(() => {});
        _noiseSuppressionCtx = null;
        _noiseSuppressionSource = null;
        _noiseSuppressionDest = null;
      }
    }
  },

  // ── Media policy ───────────────────────────────────

  getQualityController: () => (engine ? engine.getQualityController() : null),

  applyServerMediaCap: (cap, ladder) => {
    if (!engine) return;
    try {
      engine.getQualityController().setServerCap(cap, ladder);
    } catch (e) {
      console.error('[CallStore] applyServerMediaCap failed:', e);
    }
  },
}));
