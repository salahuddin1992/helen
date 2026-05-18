import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useCallStore } from '@/stores/call.store.v2';
import {
  Mic,
  MicOff,
  Camera,
  CameraOff,
  Monitor,
  PhoneOff,
  Pause,
  Play,
  Volume2,
  VolumeX,
  Edit,
} from 'lucide-react';

/** Captions / closed-caption icon (rounded badge with CC). */
const CaptionsIconSvg: React.FC<{ size?: number }> = ({ size = 24 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <rect x="2" y="5" width="20" height="14" rx="3" />
    <path d="M7 10c-1 0-2 .8-2 2s1 2 2 2" />
    <path d="M14 10c-1 0-2 .8-2 2s1 2 2 2" />
  </svg>
);

/** Background-blur icon (concentric blur rings). Inline SVG so we
 *  don't depend on a lucide name that may not be re-exported in
 *  the pinned version. */
const BlurIconSvg: React.FC<{ size?: number }> = ({ size = 24 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="10" strokeDasharray="2 3" />
    <circle cx="12" cy="12" r="6"  strokeDasharray="2 3" />
    <circle cx="12" cy="12" r="2"  fill="currentColor" stroke="none" />
  </svg>
);

/** Raise-hand icon (open palm, lucide-style stroke). Inline SVG to
 *  avoid coupling to the specific lucide-react bundle's named exports
 *  (the bare `Hand` symbol isn't re-exported in the version pinned
 *  here, only `HandIcon` / `HandMetal` / etc). */
const HandIconSvg: React.FC<{ size?: number }> = ({ size = 24 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <path d="M18 11V6a2 2 0 0 0-2-2v0a2 2 0 0 0-2 2v0" />
    <path d="M14 10V4a2 2 0 0 0-2-2v0a2 2 0 0 0-2 2v2" />
    <path d="M10 10.5V6a2 2 0 0 0-2-2v0a2 2 0 0 0-2 2v8" />
    <path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15" />
  </svg>
);

/** Push-to-talk icon (walkie-talkie style) */
const PushToTalkIcon: React.FC<{ size?: number }> = ({ size = 24 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="8" y="2" width="8" height="16" rx="2" />
    <line x1="12" y1="18" x2="12" y2="22" />
    <line x1="8" y1="22" x2="16" y2="22" />
    <circle cx="12" cy="7" r="1.5" fill="currentColor" stroke="none" />
    <line x1="10" y1="11" x2="14" y2="11" />
    <line x1="10" y1="13" x2="14" y2="13" />
  </svg>
);
import ScreenSharePicker from './ScreenSharePicker';
import QualitySelector from './QualitySelector';
import HostMenu from './HostMenu';
import ReactionsPicker from './ReactionsPicker';
import PiPButton from './PiPButton';
import BackgroundImagePicker from './BackgroundImagePicker';
import { CallRecordButton } from '@/components/call/record/CallRecordButton';
import { useAuthStore } from '@/stores/auth.store';
import { useMyChannelRole } from '@/hooks/useMyChannelRole';
import { t } from '@/i18n';

const ControlButton: React.FC<{
  icon: React.ReactNode;
  label: string;
  isActive?: boolean;
  isWarning?: boolean;
  onClick: () => void;
}> = ({ icon, label, isActive, isWarning, onClick }) => {
  const baseClasses =
    'w-14 h-14 rounded-full flex items-center justify-center transition-all duration-200 relative group';
  const stateClasses = isWarning
    ? 'bg-red-600 hover:bg-red-700 text-white'
    : isActive
      ? 'bg-surface-800 text-text-100 hover:bg-surface-700'
      : 'bg-surface-700 text-text-300 hover:bg-surface-600';

  return (
    <div className="flex flex-col items-center gap-2">
      <button
        onClick={onClick}
        className={`${baseClasses} ${stateClasses}`}
        title={label}
      >
        {icon}
        {!isActive && (
          <div className="absolute inset-0 rounded-full border-2 border-transparent group-hover:border-text-400/30 transition-colors" />
        )}
      </button>
      <span className="text-xs text-text-400 font-medium">{label}</span>
    </div>
  );
};

const CallControls: React.FC = () => {
  const {
    toggleMute,
    toggleVideo,
    toggleHand,
    startScreenShare,
    stopScreenShare,
    hangup,
    holdCall,
    resumeCall,
    isMuted,
    isVideoOff,
    isHandRaised,
    isScreenSharing,
    isOnHold,
    isPushToTalk,
    isNoiseSuppression,
    togglePushToTalk,
    toggleNoiseSuppression,
    pushToTalkKeyDown,
    pushToTalkKeyUp,
    getQualityController,
    videoEffect,
    setVideoEffect,
    liveCaptionsEnabled,
    toggleLiveCaptions,
  } = useCallStore();

  const qualityController = getQualityController();
  const channelId = (useCallStore.getState() as any).channelId as string | undefined;
  const callId = (useCallStore.getState() as any).callId as string | undefined;
  const navigate = useNavigate();

  const [showScreenPicker, setShowScreenPicker] = useState(false);

  // Whiteboard launch — uses the channelId of the active call so everyone
  // in the call lands on the same board. Falls back to the call_id if there
  // is no channel (1-to-1). The whiteboard route opens in the same window;
  // the active call banner still shows so the user can hop back to the call.
  const handleOpenWhiteboard = () => {
    const sid = channelId || callId;
    if (!sid) return;
    navigate(`/whiteboard/${sid}`);
  };

  // Push-to-talk keyboard listeners (Space key)
  const handlePTTKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (!isPushToTalk) return;
      if (e.code === 'Space' && !e.repeat) {
        e.preventDefault();
        pushToTalkKeyDown();
      }
    },
    [isPushToTalk, pushToTalkKeyDown]
  );

  const handlePTTKeyUp = useCallback(
    (e: KeyboardEvent) => {
      if (!isPushToTalk) return;
      if (e.code === 'Space') {
        e.preventDefault();
        pushToTalkKeyUp();
      }
    },
    [isPushToTalk, pushToTalkKeyUp]
  );

  useEffect(() => {
    window.addEventListener('keydown', handlePTTKeyDown);
    window.addEventListener('keyup', handlePTTKeyUp);
    return () => {
      window.removeEventListener('keydown', handlePTTKeyDown);
      window.removeEventListener('keyup', handlePTTKeyUp);
    };
  }, [handlePTTKeyDown, handlePTTKeyUp]);

  const handleScreenShare = () => {
    if (isScreenSharing) {
      // Stop screen sharing
      stopScreenShare();
    } else {
      // Show picker to select screen/window
      setShowScreenPicker(true);
    }
  };

  const handleScreenShareSelected = async (sourceId: string) => {
    await startScreenShare(sourceId);
    setShowScreenPicker(false);
  };

  const handleHangUp = () => {
    hangup();
  };

  const handleHold = () => {
    if (isOnHold) {
      resumeCall();
    } else {
      holdCall();
    }
  };

  return (
    <>
      <div className="flex items-center justify-center gap-8">
        {/* Mute/Unmute */}
        <div className="relative">
          <ControlButton
            icon={!isMuted ? <Mic size={24} /> : <MicOff size={24} />}
            label={!isMuted ? t('call.mute') : t('call.unmute')}
            isActive={!isMuted}
            onClick={() => toggleMute()}
          />
          {isPushToTalk && (
            <span className="absolute -top-1 -right-1 px-1.5 py-0.5 bg-yellow-500 text-black text-[10px] font-bold rounded-full leading-none">
              PTT
            </span>
          )}
        </div>

        {/* Camera On/Off */}
        <ControlButton
          icon={!isVideoOff ? <Camera size={24} /> : <CameraOff size={24} />}
          label={!isVideoOff ? t('call.camera_off') : t('call.camera_on')}
          isActive={!isVideoOff}
          onClick={() => toggleVideo()}
        />

        {/* Screen Share */}
        <ControlButton
          icon={<Monitor size={24} />}
          label={isScreenSharing ? t('call.stop_sharing') : t('call.share_screen')}
          isActive={isScreenSharing}
          onClick={handleScreenShare}
        />

        {/* Hold/Resume */}
        <ControlButton
          icon={isOnHold ? <Play size={24} /> : <Pause size={24} />}
          label={isOnHold ? t('call.resume') || 'Resume' : t('call.hold') || 'Hold'}
          isActive={!isOnHold}
          onClick={handleHold}
        />

        {/* Raise Hand — webinar feature. The host sees the indicator
            on the participant tile and can grant the floor. */}
        <ControlButton
          icon={<HandIconSvg size={24} />}
          label={
            isHandRaised
              ? (t('call.lower_hand') || 'إنزال اليد')
              : (t('call.raise_hand') || 'رفع اليد')
          }
          isActive={!!isHandRaised}
          onClick={() => toggleHand()}
        />

        {/* Live reactions picker — quick emoji that floats up the
            screen for everyone in the call. */}
        <ReactionsPicker />

        {/* Picture-in-Picture — pop the call into a floating window
            so the user can multitask. Chromium-only API. */}
        <PiPButton />

        {/* Custom virtual background image — preset palette + upload. */}
        <BackgroundImagePicker />

        {/* Live captions — chunks the local mic into 3s windows,
            sends each to whisper-cli on the server, and the result
            appears in the captions overlay for everyone in the call. */}
        <ControlButton
          icon={<CaptionsIconSvg size={24} />}
          label={liveCaptionsEnabled ? 'إيقاف التسميات' : 'تسميات حية'}
          isActive={liveCaptionsEnabled}
          onClick={() => toggleLiveCaptions()}
        />

        {/* Background blur toggle. Pipes the local video through a
            canvas with a Gaussian blur filter and replaces the
            outgoing track. Click cycles: none → blur → darken → none. */}
        <ControlButton
          icon={<BlurIconSvg size={24} />}
          label={
            videoEffect === 'blur'
              ? 'تشويش مفعّل'
              : videoEffect === 'darken'
                ? 'إعتام مفعّل'
                : 'تشويش الخلفية'
          }
          isActive={videoEffect !== 'none'}
          onClick={() => {
            const next = videoEffect === 'none'
              ? 'blur'
              : videoEffect === 'blur'
                ? 'darken'
                : 'none';
            void setVideoEffect(next);
          }}
        />

        {/* Push-to-Talk */}
        <ControlButton
          icon={<PushToTalkIcon size={24} />}
          label={isPushToTalk ? 'PTT On' : 'PTT Off'}
          isActive={isPushToTalk}
          onClick={() => togglePushToTalk()}
        />

        {/* Noise Suppression */}
        <ControlButton
          icon={isNoiseSuppression ? <Volume2 size={24} /> : <VolumeX size={24} />}
          label={isNoiseSuppression ? 'Denoise On' : 'Denoise Off'}
          isActive={isNoiseSuppression}
          onClick={() => toggleNoiseSuppression()}
        />

        {/* Quality selector (resolution / bitrate ladder) */}
        <div className="flex flex-col items-center gap-2">
          <QualitySelector controller={qualityController} />
          <span className="text-xs text-text-400 font-medium">Quality</span>
        </div>

        {/* Whiteboard launch — opens /whiteboard/<channelId> for the
            collaborative board; the call keeps running in the background. */}
        <ControlButton
          icon={<Edit size={24} />}
          label={t('call.whiteboard') || 'لوحة'}
          onClick={handleOpenWhiteboard}
        />

        {/* Local-only call recording — saves a .webm to the user's
            Downloads folder via the Electron downloads IPC. Off by
            default, no auto-start, never uploaded. */}
        <div className="flex flex-col items-center gap-2">
          <CallRecordButton />
          <span className="text-xs text-text-400 font-medium">Record</span>
        </div>

        {/* Hang Up */}
        <ControlButton
          icon={<PhoneOff size={24} />}
          label={t('call.end_call')}
          isWarning
          onClick={handleHangUp}
        />

        {/* Moderation menu — visible only to host or channel mod. */}
        <HostMenuMount />
      </div>

      {/* Screen Share Picker Modal */}
      {showScreenPicker && (
        <ScreenSharePicker
          onSelect={handleScreenShareSelected}
          onCancel={() => setShowScreenPicker(false)}
        />
      )}
    </>
  );
};

/**
 * HostMenuMount — small wrapper that pulls callId/hostId/participants
 * from the store and renders <HostMenu /> only for the active host.
 * Channel-moderator detection is currently coarse (via auth profile);
 * the server is the authoritative gate for moderation actions, so the
 * UI just provides hint visibility.
 */
const HostMenuMount: React.FC = () => {
  const { callId, hostId, participants, channelId } = useCallStore() as any;
  const me = useAuthStore((s) => s.user);
  // Per-channel role lookup. Returns null while loading or if we are
  // not a member — both states resolve to "no moderation buttons",
  // matching the server's enforcement gate
  // (`_is_call_moderator` on the backend).
  const channelRole = useMyChannelRole(channelId || null);
  if (!callId || !me) return null;

  const isHost = hostId === me.id;
  // Moderator status is per-channel, not global. Falls back to the
  // global User.role only for 1-to-1 calls (no channelId), where
  // there is no per-channel concept anyway.
  const isModerator = channelId
    ? channelRole === 'admin' || channelRole === 'moderator'
    : (me as any).role === 'admin' || (me as any).role === 'moderator';

  if (!isHost && !isModerator) return null;

  const others = Object.values(participants || {})
    .filter((p: any) => p?.userId && p.userId !== me.id)
    .map((p: any) => ({
      userId: p.userId,
      displayName: p.displayName || p.username || p.userId.slice(0, 8),
      isMuted: !!p.isAudioMuted,
    }));

  return (
    <HostMenu
      callId={callId}
      participants={others as any}
      isHost={isHost}
      isModerator={isModerator}
    />
  );
};

export default CallControls;
