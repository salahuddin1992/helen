/**
 * QuickCallSheet.tsx — One-tap call initiation + live call status.
 *
 * This is a bottom-sheet / popover that appears when the user taps
 * the call button in a chat or group. It shows:
 *
 * If NO active call:
 *   → Two big buttons: "Voice Call" and "Video Call"
 *   → One-tap "Share Screen" shortcut
 *
 * If call is ACTIVE in this channel:
 *   → "Join Call" button with participant preview
 *   → Live participant count + who's in the call
 *
 * If call is ACTIVE in ANOTHER channel:
 *   → Warning: "You're already in a call"
 *   → Option to leave current and join this one
 *
 * Design principles:
 *   - Maximum 1 tap to start a call
 *   - Live state: see who's already in the call before joining
 *   - Big, obvious, color-coded buttons (green = voice, blue = video)
 *   - Screen share is a first-class action, not buried in a menu
 */

import React, { useState } from 'react';
import {
  Phone, Video, AlertCircle, PhoneOff, Users, Loader2,
  X, Mic, MicOff, ChevronRight
} from 'lucide-react';
import { t } from '@/i18n';
import { useCallStore } from '@/stores/call.store.v2';
import { useChannelActiveCall } from '@/hooks/useChannelActiveCall';

interface CallParticipantPreview {
  id: string;
  displayName: string;
  avatar?: string;
  isMuted: boolean;
  hasVideo: boolean;
}

interface QuickCallSheetProps {
  isOpen: boolean;
  onClose: () => void;
  channelId: string;
  channelName: string;
  channelType: 'dm' | 'group';
  /** Participants currently in an active call on this channel */
  activeCallParticipants?: CallParticipantPreview[];
  /** Whether there's already a live call on this specific channel */
  hasActiveCall?: boolean;
  /** Target user ID for DM calls */
  targetUserId?: string;
}

const QuickCallSheet: React.FC<QuickCallSheetProps> = ({
  isOpen,
  onClose,
  channelId,
  channelName,
  channelType,
  activeCallParticipants: activeCallParticipantsProp,
  hasActiveCall: hasActiveCallProp,
  targetUserId,
}) => {
  const [isStarting, setIsStarting] = useState<'audio' | 'video' | 'screen' | null>(null);

  const callStatus = useCallStore((s) => s.status);
  const currentChannelId = useCallStore((s) => s.channelId);
  const initiateCall = useCallStore((s) => s.initiateCall);
  const initiateGroupCall = useCallStore((s) => s.initiateGroupCall);
  const startScreenShare = useCallStore((s) => s.startScreenShare);
  const hangup = useCallStore((s) => s.hangup);

  // Discovery hook — drives "Join Existing Call" UX automatically when
  // the parent doesn't pass hasActiveCall/activeCallParticipants props.
  // Backwards compatible: if the parent supplies the props (legacy
  // wiring from before the hook landed) those win.
  const discovered = useChannelActiveCall(
    channelType === 'group' ? channelId : null,
  );
  const hasActiveCall = hasActiveCallProp ?? discovered.hasActiveCall;
  const activeCallParticipants =
    activeCallParticipantsProp ?? discovered.activeCallParticipants;

  const isInCall = callStatus === 'active' || callStatus === 'connecting' || callStatus === 'ringing';
  const isInThisChannel = isInCall && currentChannelId === channelId;
  const isInOtherCall = isInCall && currentChannelId !== channelId;

  if (!isOpen) return null;

  const handleStartCall = async (mediaType: 'audio' | 'video') => {
    setIsStarting(mediaType);
    try {
      if (channelType === 'dm' && targetUserId) {
        await initiateCall(targetUserId, mediaType);
      } else {
        await initiateGroupCall(channelId, mediaType);
      }
      onClose();
    } catch {
      setIsStarting(null);
    }
  };

  const handleShareScreen = async () => {
    setIsStarting('screen');
    try {
      // Start a video call first, then trigger screen share
      if (channelType === 'dm' && targetUserId) {
        await initiateCall(targetUserId, 'video');
      } else {
        await initiateGroupCall(channelId, 'video');
      }
      // Screen share picker will be triggered from CallControls after call connects
      onClose();
    } catch {
      setIsStarting(null);
    }
  };

  const handleJoinCall = async () => {
    setIsStarting('audio');
    try {
      await initiateGroupCall(channelId, 'audio');
      onClose();
    } catch {
      setIsStarting(null);
    }
  };

  const handleSwitchCall = async () => {
    hangup();
    // Small delay to ensure cleanup, then join
    setTimeout(async () => {
      await initiateGroupCall(channelId, 'audio');
      onClose();
    }, 500);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-sm mx-4 mb-4 sm:mb-0 bg-surface-900 rounded-2xl border border-surface-800 shadow-2xl overflow-hidden animate-slide-up">

        {/* ─── Header ─── */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-surface-800">
          <div>
            <h3 className="text-base font-semibold text-white">
              {hasActiveCall
                ? (t('call.active_call') || 'Call in Progress')
                : (t('call.start_call') || 'Start a Call')}
            </h3>
            <p className="text-xs text-gray-500 mt-0.5">{channelName}</p>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-full hover:bg-surface-800 flex items-center justify-center text-gray-500 hover:text-gray-300"
          >
            <X size={18} />
          </button>
        </div>

        {/* ─── State: Active call in THIS channel → Join ─── */}
        {hasActiveCall && !isInThisChannel && (
          <div className="p-5">
            {/* Participant preview */}
            {activeCallParticipants.length > 0 && (
              <div className="mb-4">
                <p className="text-xs text-gray-500 mb-2">
                  {activeCallParticipants.length} {t('call.people_in_call') || 'people in this call'}
                </p>
                <div className="flex flex-wrap gap-2">
                  {activeCallParticipants.slice(0, 6).map((p) => (
                    <div
                      key={p.id}
                      className="flex items-center gap-2 px-2.5 py-1.5 bg-surface-800 rounded-lg"
                    >
                      <div className="w-6 h-6 rounded-full bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white text-[10px] font-bold">
                        {p.displayName.charAt(0).toUpperCase()}
                      </div>
                      <span className="text-xs text-gray-300 max-w-[80px] truncate">
                        {p.displayName}
                      </span>
                      {p.isMuted && <MicOff size={10} className="text-red-400" />}
                    </div>
                  ))}
                  {activeCallParticipants.length > 6 && (
                    <div className="flex items-center px-2.5 py-1.5 bg-surface-800 rounded-lg text-xs text-gray-500">
                      +{activeCallParticipants.length - 6}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Join button — BIG and obvious */}
            <button
              onClick={handleJoinCall}
              disabled={!!isStarting}
              className="w-full py-4 bg-green-600 hover:bg-green-700 disabled:bg-green-600/50 text-white font-bold rounded-xl transition-colors flex items-center justify-center gap-3 text-lg"
            >
              {isStarting ? (
                <Loader2 size={22} className="animate-spin" />
              ) : (
                <Phone size={22} />
              )}
              {t('call.join_call') || 'Join Call'}
            </button>
          </div>
        )}

        {/* ─── State: Already in another call → Warning ─── */}
        {isInOtherCall && (
          <div className="p-5">
            <div className="p-3 bg-yellow-600/10 border border-yellow-600/20 rounded-xl mb-4">
              <p className="text-sm text-yellow-400">
                {t('call.already_in_call') || "You're already in another call"}
              </p>
            </div>

            <button
              onClick={handleSwitchCall}
              className="w-full py-3.5 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl transition-colors flex items-center justify-center gap-2 text-base"
            >
              <PhoneOff size={16} />
              {t('call.leave_and_join') || 'Leave current & join here'}
            </button>
          </div>
        )}

        {/* ─── State: Already in THIS channel's call → Show controls ─── */}
        {isInThisChannel && (
          <div className="p-5 text-center">
            <div className="w-12 h-12 rounded-full bg-green-600/20 flex items-center justify-center mx-auto mb-3">
              <Phone size={24} className="text-green-400" />
            </div>
            <p className="text-sm text-green-400 font-medium">
              {t('call.youre_in_call') || "You're in this call"}
            </p>
            <p className="text-xs text-gray-500 mt-1">
              {t('call.use_controls') || 'Use the call controls above'}
            </p>
          </div>
        )}

        {/* ─── State: No active call → Start options ─── */}
        {!hasActiveCall && !isInCall && (
          <div className="p-5 space-y-3">
            {/* Voice call — big green button */}
            <button
              onClick={() => handleStartCall('audio')}
              disabled={!!isStarting}
              className="w-full py-4 bg-green-600 hover:bg-green-700 disabled:bg-green-600/50 text-white font-semibold rounded-xl transition-colors flex items-center justify-center gap-3 text-base"
            >
              {isStarting === 'audio' ? (
                <Loader2 size={20} className="animate-spin" />
              ) : (
                <Phone size={20} />
              )}
              {t('call.voice_call') || 'Voice Call'}
            </button>

            {/* Video call — big blue button */}
            <button
              onClick={() => handleStartCall('video')}
              disabled={!!isStarting}
              className="w-full py-4 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-600/50 text-white font-semibold rounded-xl transition-colors flex items-center justify-center gap-3 text-base"
            >
              {isStarting === 'video' ? (
                <Loader2 size={20} className="animate-spin" />
              ) : (
                <Video size={20} />
              )}
              {t('call.video_call') || 'Video Call'}
            </button>

            {/* Screen share — purple accent */}
            <button
              onClick={handleShareScreen}
              disabled={!!isStarting}
              className="w-full py-3.5 bg-surface-800 hover:bg-surface-700 disabled:bg-surface-800/50 text-gray-300 font-medium rounded-xl transition-colors flex items-center justify-center gap-3 text-sm border border-surface-700"
            >
              {isStarting === 'screen' ? (
                <Loader2 size={18} className="animate-spin" />
              ) : (
                <AlertCircle size={18} className="text-purple-400" />
              )}
              {t('call.share_screen') || 'Share Screen'}
            </button>

            {/* Hint */}
            <p className="text-center text-xs text-gray-600 pt-1">
              {channelType === 'group'
                ? (t('call.group_hint') || 'Everyone in the group will be notified')
                : (t('call.dm_hint') || 'This will ring the other person')
              }
            </p>
          </div>
        )}
      </div>
    </div>
  );
};

export default QuickCallSheet;
