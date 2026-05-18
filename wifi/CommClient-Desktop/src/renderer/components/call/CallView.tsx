import React, { useEffect, useState, useRef, useCallback } from 'react';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';
import { Phone } from 'lucide-react';
import CallControls from './CallControls';
import ActiveSpeakerIndicator from './ActiveSpeakerIndicator';
import ConnectionQualityIndicator from './ConnectionQualityIndicator';
import { CallEngine } from '@/services/call';
import { t } from '@/i18n';
import { useSpotlightStore } from '@/stores/spotlight.store';
import { NetworkAdaptiveVideoMount } from '@/components/call/adaptive/NetworkAdaptiveVideoMount';
import ReactionsLayer from '@/components/call/ReactionsLayer';
import RaisedHandsPanel from '@/components/call/RaisedHandsPanel';
import PeerVolumeSlider from '@/components/call/PeerVolumeSlider';
import ConnectionStatsOverlay from '@/components/call/ConnectionStatsOverlay';
import CaptionsOverlay from '@/components/call/CaptionsOverlay';
import ParticipantSearch from '@/components/call/ParticipantSearch';
import LobbyPanel from '@/components/call/LobbyPanel';
import QAPanel from '@/components/call/QAPanel';
import PasscodeBadge from '@/components/call/PasscodeBadge';
import RecordingBanner from '@/components/call/RecordingBanner';
import LayoutSwitcher from '@/components/call/LayoutSwitcher';
import { useLayoutStore } from '@/stores/layout.store';
import AutoMuteWhileTyping from '@/components/call/AutoMuteWhileTyping';
import WhisperPanel from '@/components/call/WhisperPanel';
import BreakoutPanel from '@/components/call/BreakoutPanel';
import HostMassActions from '@/components/call/HostMassActions';
import CoHostMount from '@/components/call/CoHostMount';
import WatchParty from '@/components/call/WatchParty';
import WindowControlsExtra from '@/components/call/WindowControlsExtra';
import AutoFollowSpeaker from '@/components/call/AutoFollowSpeaker';

interface ParticipantGridProps {
  participants: Array<{
    id: string;
    name: string;
    videoStream?: MediaStream;
    audioEnabled: boolean;
    videoEnabled: boolean;
    isScreenSharing: boolean;
    isHandRaised?: boolean;
  }>;
}

/** Hidden mixer — one <audio> element per remote peer with its
 *  ``volume`` bound to the per-peer slider state, so users can
 *  individually rebalance loud participants. The visible <video>
 *  for each peer is muted (we don't want double-playback).
 *  Without this, the per-peer slider has nothing to act on. */
const PeerAudioMixer: React.FC = () => {
  const remoteStreams = useCallStore((s) => s.remoteStreams);
  const peerVolumes = useCallStore((s) => s.peerVolumes);
  const audioRefs = useRef<Record<string, HTMLAudioElement>>({});

  useEffect(() => {
    // Sync srcObject for new peers + clean up gone peers.
    for (const peerId of Object.keys(remoteStreams)) {
      const el = audioRefs.current[peerId];
      if (el && (el as any).srcObject !== remoteStreams[peerId]) {
        (el as any).srcObject = remoteStreams[peerId];
      }
    }
    for (const peerId of Object.keys(audioRefs.current)) {
      if (!remoteStreams[peerId]) {
        delete audioRefs.current[peerId];
      }
    }
  }, [remoteStreams]);

  useEffect(() => {
    // Apply volume on every change. The audio.volume API caps at 1.0;
    // browsers ignore values above that, so the slider's 1.0–1.5
    // range is effectively a no-op past 1. That's intentional — true
    // amplification needs a Web Audio GainNode chain, which can be
    // wired later if users actually need >100%.
    for (const peerId of Object.keys(audioRefs.current)) {
      const el = audioRefs.current[peerId];
      if (!el) continue;
      const v = peerVolumes[peerId] ?? 1;
      el.volume = Math.max(0, Math.min(1, v));
      el.muted = v === 0;
    }
  }, [peerVolumes, remoteStreams]);

  return (
    <div className="hidden" aria-hidden="true">
      {Object.keys(remoteStreams).map((peerId) => (
        <audio
          key={peerId}
          autoPlay
          ref={(el) => {
            if (el) audioRefs.current[peerId] = el;
          }}
        />
      ))}
    </div>
  );
};

const ParticipantVideo: React.FC<{
  participant: ParticipantGridProps['participants'][0];
  isLocal?: boolean;
  isSpeaking?: boolean;
}> = ({ participant, isLocal, isSpeaking }) => {
  const videoRef = React.useRef<HTMLVideoElement>(null);
  const [videoReady, setVideoReady] = useState(false);

  useEffect(() => {
    if (!videoRef.current || !participant.videoStream) return;

    videoRef.current.srcObject = participant.videoStream;
    videoRef.current.onloadedmetadata = () => setVideoReady(true);

    return () => {
      if (videoRef.current) {
        videoRef.current.srcObject = null;
      }
    };
  }, [participant.videoStream]);

  return (
    <div className={`relative w-full h-full bg-surface-950 rounded-lg overflow-hidden transition-shadow duration-300 ${isSpeaking ? 'ring-2 ring-green-500 shadow-[0_0_15px_rgba(34,197,94,0.4)]' : ''}`}>
      {participant.videoEnabled && videoReady ? (
        <video
          ref={videoRef}
          autoPlay
          playsInline
          // Visible <video> stays muted — the PeerAudioMixer plays
          // remote audio through dedicated <audio> elements so the
          // per-peer volume slider has something to control. Local
          // tile is also muted to avoid feedback.
          muted={true}
          className="w-full h-full object-cover"
        />
      ) : (
        <div className="w-full h-full flex flex-col items-center justify-center bg-gradient-to-br from-surface-900 to-surface-950">
          <div className="w-20 h-20 rounded-full bg-surface-800 flex items-center justify-center text-3xl font-bold text-text-200">
            {participant.name.charAt(0).toUpperCase()}
          </div>
          {!participant.videoEnabled && (
            <p className="mt-4 text-sm text-text-400">{t('call.camera_off')}</p>
          )}
        </div>
      )}

      {/* Raise-hand indicator — corner badge so the host can spot
          raised hands at a glance even on busy grids. Animated to
          draw attention without being obnoxious. */}
      {participant.isHandRaised && (
        <div className="absolute top-2 left-2 z-10 px-2 py-1 rounded-full bg-yellow-400/95 text-yellow-950 text-xs font-bold shadow-lg flex items-center gap-1 animate-pulse">
          <span aria-hidden="true">✋</span>
          <span>{t('call.hand_raised') || 'يد مرفوعة'}</span>
        </div>
      )}

      {/* Per-peer volume slider — appears on hover. Local tile
          doesn't get one because muting yourself is what the main
          mute button is for. */}
      {!isLocal && (
        <div className="absolute top-2 right-2 z-10">
          <PeerVolumeSlider peerId={participant.id} />
        </div>
      )}

      <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/60 to-transparent p-3">
        <p className="text-white text-sm font-medium truncate">
          {participant.name}
          {isLocal && (
            <span className="text-xs text-text-400 ml-2">({t('call.you')})</span>
          )}
        </p>
        <div className="flex gap-2 mt-1">
          {!participant.audioEnabled && (
            <span className="inline-block px-2 py-0.5 bg-red-500/80 rounded text-xs text-white">
              {t('call.muted')}
            </span>
          )}
          {participant.isScreenSharing && (
            <span className="inline-block px-2 py-0.5 bg-blue-500/80 rounded text-xs text-white">
              {t('call.sharing')}
            </span>
          )}
        </div>
      </div>
    </div>
  );
};

// ── SpotlightAwareGrid ────────────────────────────────────────────
//
// When ``useSpotlightStore.spotlightedPeerId`` is set, render the
// spotlit participant in a big tile + everyone else as thumbnails
// at the bottom. Otherwise fall back to the normal equal grid.
// Each tile is click-to-toggle, so the host can pin/un-pin without
// leaving the call.
const SpotlightAwareGrid: React.FC<{
  participantList: any[];
  remoteStreams: Record<string, MediaStream>;
  speakingPeers: Record<string, boolean>;
  qualityLevel: string;
  gridColsClass: string;
  isScrollable: boolean;
  layoutMode: 'gallery' | 'speaker' | 'sidebar';
  dominantSpeaker: string | null;
}> = ({
  participantList, remoteStreams, speakingPeers,
  qualityLevel, gridColsClass, isScrollable,
  layoutMode, dominantSpeaker,
}) => {
  const spotlightedId = useSpotlightStore((s) => s.spotlightedPeerId);
  const toggleSpotlight = useSpotlightStore((s) => s.toggleSpotlight);

  if (participantList.length === 0) {
    return (
      <div className="flex items-center justify-center bg-surface-900 rounded-lg h-full">
        <div className="text-center">
          <p className="text-text-400 mb-2">{t('call.waiting')}</p>
          <p className="text-sm text-text-500">
            {t('call.participants_will_join')}
          </p>
        </div>
      </div>
    );
  }

  // Layout-mode-aware focus selection.
  //
  // Priority order for picking the "main" tile:
  //   1. Explicit spotlight via the spotlight store.
  //   2. Dominant speaker (computed from audio levels in CallView).
  //   3. First participant — fallback so we never render nothing.
  //
  // ``speaker`` mode hides thumbnails entirely; ``sidebar`` puts them
  // in a vertical strip. ``gallery`` falls through to the existing
  // equal-grid behaviour at the bottom.
  const focusedId =
    spotlightedId ??
    (layoutMode !== 'gallery' ? dominantSpeaker : null);
  const spotlit = focusedId
    ? participantList.find((p) => p.peerId === focusedId)
    : null;

  // ── speaker / sidebar layouts ───────────────────────────────
  if (layoutMode === 'speaker' && spotlit) {
    return (
      <button
        type="button"
        onClick={() => toggleSpotlight(spotlit.peerId)}
        className="w-full h-full bg-surface-900 rounded-lg overflow-hidden
                   relative ring-2 ring-blue-500/40 cursor-zoom-out"
        title="إلغاء التركيز"
      >
        <ParticipantVideo
          participant={{
            id: spotlit.peerId,
            name: spotlit.displayName || spotlit.peerId,
            videoStream: remoteStreams[spotlit.peerId] || undefined,
            audioEnabled: !spotlit.isAudioMuted,
            videoEnabled: !spotlit.isVideoOff,
            isScreenSharing: spotlit.isSharingScreen,
            isHandRaised: !!spotlit.isHandRaised,
          }}
          isSpeaking={speakingPeers[spotlit.peerId] || false}
        />
        <div className="absolute top-2 right-2 z-10">
          <ConnectionQualityIndicator quality={mapQualityLevel(qualityLevel)} />
        </div>
      </button>
    );
  }

  if (layoutMode === 'sidebar' && spotlit) {
    const others = participantList.filter((p) => p.peerId !== spotlit.peerId);
    return (
      <div className="flex h-full gap-3">
        <button
          type="button"
          onClick={() => toggleSpotlight(spotlit.peerId)}
          className="flex-1 bg-surface-900 rounded-lg overflow-hidden
                     relative ring-2 ring-blue-500/40 cursor-zoom-out"
          title="إلغاء التركيز"
        >
          <ParticipantVideo
            participant={{
              id: spotlit.peerId,
              name: spotlit.displayName || spotlit.peerId,
              videoStream: remoteStreams[spotlit.peerId] || undefined,
              audioEnabled: !spotlit.isAudioMuted,
              videoEnabled: !spotlit.isVideoOff,
              isScreenSharing: spotlit.isSharingScreen,
              isHandRaised: !!spotlit.isHandRaised,
            }}
            isSpeaking={speakingPeers[spotlit.peerId] || false}
          />
        </button>
        {others.length > 0 && (
          <div className="w-40 flex-none flex flex-col gap-2 overflow-y-auto">
            {others.map((p) => (
              <button
                key={p.peerId}
                type="button"
                onClick={() => toggleSpotlight(p.peerId)}
                className="aspect-video bg-surface-900 rounded-lg
                           overflow-hidden cursor-zoom-in hover:ring-2
                           hover:ring-blue-400 transition flex-none"
                title="تركيز على هذا المشارك"
              >
                <ParticipantVideo
                  participant={{
                    id: p.peerId,
                    name: p.displayName || p.peerId,
                    videoStream: remoteStreams[p.peerId] || undefined,
                    audioEnabled: !p.isAudioMuted,
                    videoEnabled: !p.isVideoOff,
                    isScreenSharing: p.isSharingScreen,
                    isHandRaised: !!p.isHandRaised,
                  }}
                  isSpeaking={speakingPeers[p.peerId] || false}
                />
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  // Spotlight mode — only when the spotlit ID is still in the
  // participant list (left calls clear automatically; this guards
  // against a stale store value).
  if (spotlit) {
    const others = participantList.filter(
      (p) => p.peerId !== spotlit.peerId,
    );
    return (
      <div className="flex flex-col h-full gap-3">
        <button
          type="button"
          onClick={() => toggleSpotlight(spotlit.peerId)}
          className="flex-1 bg-surface-900 rounded-lg overflow-hidden
                     relative ring-2 ring-blue-500 cursor-zoom-out"
          title="إلغاء التركيز"
        >
          <ParticipantVideo
            participant={{
              id: spotlit.peerId,
              name: spotlit.displayName || spotlit.peerId,
              videoStream: remoteStreams[spotlit.peerId] || undefined,
              audioEnabled: !spotlit.isAudioMuted,
              videoEnabled: !spotlit.isVideoOff,
              isScreenSharing: spotlit.isSharingScreen,
              isHandRaised: !!spotlit.isHandRaised,
            }}
            isSpeaking={speakingPeers[spotlit.peerId] || false}
          />
          <div className="absolute top-2 right-2 z-10">
            <ConnectionQualityIndicator
              quality={mapQualityLevel(qualityLevel)}
            />
          </div>
        </button>

        {others.length > 0 && (
          <div className="flex gap-2 overflow-x-auto h-24 flex-none">
            {others.map((p) => (
              <button
                key={p.peerId}
                type="button"
                onClick={() => toggleSpotlight(p.peerId)}
                className="flex-none w-32 h-full bg-surface-900
                           rounded-lg overflow-hidden cursor-zoom-in
                           hover:ring-2 hover:ring-blue-400 transition"
                title="تركيز على هذا المشارك"
              >
                <ParticipantVideo
                  participant={{
                    id: p.peerId,
                    name: p.displayName || p.peerId,
                    videoStream: remoteStreams[p.peerId] || undefined,
                    audioEnabled: !p.isAudioMuted,
                    videoEnabled: !p.isVideoOff,
                    isScreenSharing: p.isSharingScreen,
                    isHandRaised: !!p.isHandRaised,
                  }}
                  isSpeaking={speakingPeers[p.peerId] || false}
                />
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  // Default: equal grid, click any tile to spotlight it.
  return (
    <div className={`grid ${gridColsClass} gap-4 ${isScrollable ? 'overflow-y-auto max-h-full' : 'h-full'}`}>
      {participantList.map((participant) => (
        <button
          type="button"
          key={participant.peerId}
          onClick={() => toggleSpotlight(participant.peerId)}
          className="bg-surface-900 rounded-lg overflow-hidden relative
                     cursor-zoom-in text-start hover:ring-2
                     hover:ring-blue-400 transition"
          title="تركيز على هذا المشارك"
        >
          <ParticipantVideo
            participant={{
              id: participant.peerId,
              name: participant.displayName || participant.peerId,
              videoStream: remoteStreams[participant.peerId] || undefined,
              audioEnabled: !participant.isAudioMuted,
              videoEnabled: !participant.isVideoOff,
              isScreenSharing: participant.isSharingScreen,
              isHandRaised: !!participant.isHandRaised,
            }}
            isSpeaking={speakingPeers[participant.peerId] || false}
          />
          <div className="absolute top-2 right-2 z-10">
            <ConnectionQualityIndicator
              quality={mapQualityLevel(qualityLevel)}
            />
          </div>
        </button>
      ))}
    </div>
  );
};

// Map engine QualityLevel to ConnectionQualityIndicator quality prop
const mapQualityLevel = (level: string): 'excellent' | 'good' | 'fair' | 'poor' | 'unknown' => {
  switch (level) {
    case 'excellent': return 'excellent';
    case 'good': return 'good';
    case 'fair': return 'fair';
    case 'poor':
    case 'critical': return 'poor';
    default: return 'unknown';
  }
};

const CallView: React.FC = () => {
  const {
    participants,
    callDuration,
    status,
    localStream,
    isScreenSharing,
    remoteStreams,
    qualityLevel,
  } = useCallStore();
  const { user } = useAuthStore();
  const layoutMode = useLayoutStore((s) => s.layout);
  const [displayTime, setDisplayTime] = useState('00:00');
  const [isDraggingLocalVideo, setIsDraggingLocalVideo] = useState(false);
  const [localVideoPosition, setLocalVideoPosition] = useState({ x: 0, y: 0 });
  const [speakingPeers, setSpeakingPeers] = useState<Record<string, boolean>>({});
  const [dominantSpeaker, setDominantSpeaker] = useState<string | null>(null);
  const audioMonitorsRef = useRef<Map<string, { getLevel: () => number; stop: () => void }>>(new Map());

  useEffect(() => {
    const minutes = Math.floor(callDuration / 60);
    const seconds = callDuration % 60;
    setDisplayTime(
      `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    );
  }, [callDuration]);

  // Drop the spotlight when the CallView unmounts so a new call
  // doesn't open with a stale spotlit peer from the previous one.
  useEffect(() => {
    return () => {
      useSpotlightStore.getState().clear();
    };
  }, []);

  // Audio level monitoring for speaking indicators.
  //
  // Single shared AudioContext for the whole call instead of one per
  // peer. Browsers cap concurrent contexts at 6 on Chromium and each
  // one allocates a worklet thread + audio graph; opening 50 of them
  // for an SFU_LARGE call locks the audio engine. The shared context
  // is created on first use, reused across every peer's analyser,
  // and torn down only on call end.
  const sharedAudioCtxRef = useRef<AudioContext | null>(null);

  useEffect(() => {
    if (status !== 'active') return;

    // Lazy-init the shared context; reuse across all peers.
    if (!sharedAudioCtxRef.current) {
      try {
        sharedAudioCtxRef.current = new AudioContext();
      } catch {
        // AudioContext creation can fail when the renderer is in a
        // background tab — abort, the next active render will retry.
        return;
      }
    }
    const ctx = sharedAudioCtxRef.current;
    if (!ctx) return;

    const monitors = audioMonitorsRef.current;
    const streamIds = Object.keys(remoteStreams);

    // Create analysers for new streams.
    for (const peerId of streamIds) {
      if (!monitors.has(peerId) && remoteStreams[peerId]) {
        try {
          const source = ctx.createMediaStreamSource(remoteStreams[peerId]);
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 256;
          source.connect(analyser);
          const dataArray = new Uint8Array(analyser.frequencyBinCount);

          monitors.set(peerId, {
            getLevel: () => {
              analyser.getByteFrequencyData(dataArray);
              let sum = 0;
              for (let i = 0; i < dataArray.length; i++) sum += dataArray[i];
              return sum / (dataArray.length * 255);
            },
            stop: () => {
              // Disconnect the analyser graph but DON'T close the
              // shared context — other peers still use it.
              try { source.disconnect(); } catch { /* idempotent */ }
              try { analyser.disconnect(); } catch { /* idempotent */ }
            },
          });
        } catch {
          // createMediaStreamSource may fail on streams without
          // audio tracks; skip silently.
        }
      }
    }

    // Remove analysers for streams that left.
    for (const [peerId, monitor] of monitors.entries()) {
      if (!streamIds.includes(peerId)) {
        monitor.stop();
        monitors.delete(peerId);
      }
    }

    // Poll audio levels.
    let rafId: number;
    const SPEAKING_THRESHOLD = 0.05;
    const poll = () => {
      const speaking: Record<string, boolean> = {};
      let maxLevel = 0;
      let maxPeer: string | null = null;

      for (const [peerId, monitor] of monitors.entries()) {
        const level = monitor.getLevel();
        const isSpeaking = level > SPEAKING_THRESHOLD;
        speaking[peerId] = isSpeaking;
        if (level > maxLevel) {
          maxLevel = level;
          maxPeer = peerId;
        }
      }

      setSpeakingPeers(speaking);
      setDominantSpeaker(maxLevel > SPEAKING_THRESHOLD ? maxPeer : null);
      rafId = requestAnimationFrame(poll);
    };
    rafId = requestAnimationFrame(poll);

    return () => {
      cancelAnimationFrame(rafId);
      // Disconnect every per-peer analyser.
      for (const [, monitor] of monitors.entries()) {
        monitor.stop();
      }
      monitors.clear();
    };
  }, [status, Object.keys(remoteStreams).join(',')]);

  // Final cleanup — close the shared AudioContext when the call ends
  // (status leaves 'active' / 'reconnecting'). Without this the
  // context survives across calls and accumulates GC pressure.
  useEffect(() => {
    if (status !== 'active' && status !== 'reconnecting') {
      const ctx = sharedAudioCtxRef.current;
      if (ctx) {
        ctx.close().catch(() => { /* ignore */ });
        sharedAudioCtxRef.current = null;
      }
    }
  }, [status]);

  // Treat both 'active' and 'reconnecting' as "in a call" so the
  // user keeps seeing tiles + controls while WebRTC is recovering.
  // Without this, a transient disconnect blanks the call UI for 5–
  // 30 seconds and the user assumes the call dropped.
  const isCallActive = status === 'active' || status === 'reconnecting';
  const isReconnecting = status === 'reconnecting';

  // Convert participants Record to array for rendering
  const participantList = Object.values(participants);

  // Find screen share stream from remote streams if any participant is sharing
  const screenSharePeerId = Object.entries(participants).find(([_, p]) => p.isSharingScreen)?.[0];
  const screenStream = screenSharePeerId ? remoteStreams[screenSharePeerId] : null;

  // The screen-share <video> ref + its srcObject effect MUST be declared
  // before the early return below — otherwise on a render where
  // `!isCallActive` we'd skip these hooks and the next active render
  // would re-introduce them, breaking React's stable hook order. (This
  // was a latent runtime crash that ESLint react-hooks/rules-of-hooks
  // caught when the lint pipeline came back online.)
  const screenShareVideoRef = React.useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (screenShareVideoRef.current && screenStream) {
      (screenShareVideoRef.current as any).srcObject = screenStream;
    }
  }, [screenStream]);

  if (!isCallActive || !user) {
    return (
      <div className="w-full h-screen bg-surface-950 flex items-center justify-center">
        <div className="text-center">
          <Phone className="w-16 h-16 text-text-400 mx-auto mb-4 opacity-50" />
          <p className="text-text-400">{t('call.no_active_call')}</p>
        </div>
      </div>
    );
  }

  const count = participantList.length;
  const gridColsClass =
    count <= 1
      ? 'grid-cols-1'
      : count <= 4
        ? 'grid-cols-2'
        : count <= 9
          ? 'grid-cols-3'
          : 'grid-cols-4';
  const isScrollable = count > 16;

  const handleLocalVideoDrag = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!isDraggingLocalVideo) return;

    const newX = e.clientX - 120;
    const newY = e.clientY - 90;

    setLocalVideoPosition({
      x: Math.max(0, Math.min(newX, window.innerWidth - 240)),
      y: Math.max(0, Math.min(newY, window.innerHeight - 180)),
    });
  };

  return (
    <div
      className="w-full h-screen bg-surface-950 flex flex-col"
      onMouseMove={handleLocalVideoDrag}
    >
      {/* Auto-pause-video-on-poor-network adapter. Renders a small
          banner when active; nothing visible otherwise. */}
      <NetworkAdaptiveVideoMount />

      {/* Reconnecting banner — shows when the call FSM is in
          'reconnecting' state (peer connection lost, ICE restart in
          progress). Without this, users see frozen tiles with no
          explanation and assume the call dropped. */}
      {isReconnecting && (
        <div className="absolute top-0 left-0 right-0 z-30 flex justify-center pointer-events-none">
          <div className="mt-3 px-4 py-2 rounded-full bg-yellow-500/95 text-yellow-950 text-sm font-medium shadow-lg flex items-center gap-2">
            <span className="inline-block w-2 h-2 rounded-full bg-yellow-900 animate-pulse" />
            {t('call.reconnecting')}
          </div>
        </div>
      )}

      {/* Floating reaction layer — receives call:reaction socket
          events and animates emojis upward across the screen. */}
      <ReactionsLayer />

      {/* FIFO queue of raised hands — host-only. Empty otherwise. */}
      <RaisedHandsPanel />

      {/* Connection stats — toggleable via chip in top-left or
          Ctrl+Shift+S. Shows per-peer RTT/jitter/loss/bitrate. */}
      <ConnectionStatsOverlay />

      {/* Live captions — bottom strip showing the last few lines of
          whisper transcription. Renders nothing when captions are
          off and the buffer is empty. */}
      <CaptionsOverlay />

      {/* Searchable participant list — Ctrl+/ or top-right chip.
          Critical at 50+ participants where finding one user by
          scrolling tiles is impractical. */}
      <ParticipantSearch />

      {/* Lobby / knock-to-enter — host-only panel. */}
      <LobbyPanel />

      {/* Q&A + polls side panel — bottom-right toggle. */}
      <QAPanel />

      {/* Per-call passcode chip — host can set/clear PIN. */}
      <PasscodeBadge />

      {/* Recording consent banner — non-dismissible, top-of-screen
          REC indicator while the call is being recorded. */}
      <RecordingBanner />

      {/* Layout switcher — gallery / speaker / sidebar. */}
      <LayoutSwitcher />

      {/* Auto-mute while typing (headless privacy helper). */}
      <AutoMuteWhileTyping />

      {/* Whisper-to-host private channel. */}
      <WhisperPanel />

      {/* Breakout rooms (host) / assignment badge (non-host). */}
      <BreakoutPanel />

      {/* Host mass-action chips (mute-all, video-off-all, etc.) +
          headless receiver listener for host_force events. */}
      <HostMassActions />

      {/* Co-host store sync + local "you are a co-host" badge. */}
      <CoHostMount />

      {/* Watch party — synchronized video playback for the call. */}
      <WatchParty />

      {/* Window pin + compact-mode toggle (Electron-only). */}
      <WindowControlsExtra />

      {/* Auto-follow active speaker — opt-in headless behaviour. */}
      <AutoFollowSpeaker />

      {/* Hidden mixer — one <audio> element per remote peer so the
          PeerVolumeSlider on each tile has audio to throttle. */}
      <PeerAudioMixer />

      {/* Main video grid */}
      <div className="flex-1 p-4 overflow-hidden">
        {isScreenSharing && screenStream ? (
          <div className="w-full h-full bg-black rounded-lg overflow-hidden relative">
            <video
              ref={screenShareVideoRef}
              autoPlay
              playsInline
              className="w-full h-full object-contain"
            />
            <div className="absolute top-4 right-4 px-3 py-1 bg-blue-500/90 rounded-full text-white text-sm font-medium">
              {t('call.screen_share')}
            </div>
          </div>
        ) : (
          <SpotlightAwareGrid
            participantList={participantList}
            remoteStreams={remoteStreams}
            speakingPeers={speakingPeers}
            qualityLevel={qualityLevel}
            gridColsClass={gridColsClass}
            isScrollable={isScrollable}
            layoutMode={layoutMode}
            dominantSpeaker={dominantSpeaker}
          />
        )}
      </div>

      {/* Local video preview (draggable) */}
      {localStream && (
        <div
          className="absolute bottom-32 right-4 w-60 h-44 bg-surface-900 rounded-lg overflow-hidden border-2 border-surface-800 cursor-move shadow-2xl transition-shadow hover:shadow-surface-700/50"
          style={{
            transform: `translate(${localVideoPosition.x}px, ${localVideoPosition.y}px)`,
          }}
          onMouseDown={() => setIsDraggingLocalVideo(true)}
          onMouseUp={() => setIsDraggingLocalVideo(false)}
          onMouseLeave={() => setIsDraggingLocalVideo(false)}
        >
          <video
            autoPlay
            playsInline
            muted
            className="w-full h-full object-cover"
            ref={(el) => {
              if (el && localStream) {
                (el as any).srcObject = localStream;
              }
            }}
          />
          <div className="absolute top-2 right-2 z-10">
            <ConnectionQualityIndicator
              quality={mapQualityLevel(qualityLevel)}
            />
          </div>
          <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/60 to-transparent p-2">
            <p className="text-white text-xs font-medium truncate">
              {user.display_name} ({t('call.you')})
            </p>
          </div>
        </div>
      )}

      {/* Dominant speaker indicator */}
      {dominantSpeaker && participants[dominantSpeaker] && (
        <div className="absolute top-4 left-1/2 -translate-x-1/2 z-10">
          <ActiveSpeakerIndicator
            displayName={participants[dominantSpeaker].displayName || dominantSpeaker}
            isActive={true}
          />
        </div>
      )}

      {/* Call timer and controls */}
      <div className="bg-surface-900/95 border-t border-surface-800 px-6 py-4">
        <div className="flex items-center justify-between">
          <div className="flex-1">
            <div className="text-3xl font-mono font-bold text-text-100">
              {displayTime}
            </div>
            <p className="text-sm text-text-400 mt-1">
              {participantList.length} {t('call.participants')}
            </p>
          </div>

          <CallControls />
        </div>
      </div>
    </div>
  );
};

export default CallView;
