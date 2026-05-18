/**
 * HostMenu — moderation controls visible only when the current user
 * is the call host or a channel moderator. Wraps the three new socket
 * events (call_kick_participant / call_force_mute /
 * call_end_for_everyone) into a small popover triggered from
 * CallControls.
 *
 * Authorization is enforced server-side; this component is purely UX.
 * Buttons are still rendered for non-hosts when explicitly requested
 * (e.g. force-mute "all" requires host); we just let the server's
 * 403 propagate as a toast rather than guessing client-side.
 */
import React, { useState } from 'react';
import { Shield, UserMinus, MicOff, PhoneOff } from 'lucide-react';
import { socketManager } from '@/services/socket.manager';
import { AppLogger } from '@/services/AppLogger';

const _log = AppLogger.create('HostMenu');

export interface HostMenuProps {
  callId: string;
  /** Live participant list, excluding self. */
  participants: Array<{ userId: string; displayName: string; isMuted?: boolean }>;
  /** Current user is host (initiator). */
  isHost: boolean;
  /** Current user holds an admin/moderator role in the call's channel. */
  isModerator: boolean;
  /** Toast helper — the parent provides this so we don't depend on
   *  any specific notification library. */
  onToast?: (message: string, kind?: 'info' | 'error' | 'success') => void;
}

const HostMenu: React.FC<HostMenuProps> = ({
  callId,
  participants,
  isHost,
  isModerator,
  onToast,
}) => {
  const [open, setOpen] = useState(false);
  const canModerate = isHost || isModerator;

  if (!canModerate) return null;

  const showToast = (msg: string, kind: 'info' | 'error' | 'success' = 'info') => {
    if (onToast) onToast(msg, kind);
    else _log.info(msg, { kind });
  };

  const handleKick = async (targetUserId: string, displayName: string) => {
    try {
      const r = await socketManager.emit('call_kick_participant', {
        call_id: callId,
        target_user_id: targetUserId,
      });
      if (r?.error) {
        showToast(`Couldn't remove ${displayName}: ${r.error}`, 'error');
      } else {
        showToast(`${displayName} removed from call`, 'success');
      }
    } catch (e) {
      showToast(`Kick failed: ${(e as Error).message}`, 'error');
    }
  };

  const handleForceMute = async (
    targetUserId: string,
    displayName: string,
    muted: boolean,
  ) => {
    try {
      const r = await socketManager.emit('call_force_mute', {
        call_id: callId,
        target_user_id: targetUserId,
        muted,
      });
      if (r?.error) {
        showToast(`Couldn't ${muted ? 'mute' : 'unmute'} ${displayName}: ${r.error}`, 'error');
      } else {
        showToast(
          `${displayName} ${muted ? 'muted' : 'unmuted'}`,
          'success',
        );
      }
    } catch (e) {
      showToast(`Force-mute failed: ${(e as Error).message}`, 'error');
    }
  };

  const handleEndForEveryone = async () => {
    if (!isHost) {
      showToast('Only the host can end the call for everyone', 'error');
      return;
    }
    if (!confirm('End this call for ALL participants?')) return;
    try {
      const r = await socketManager.emit('call_end_for_everyone', {
        call_id: callId,
        reason: 'host_ended_for_everyone',
      });
      if (r?.error) {
        showToast(`Couldn't end call: ${r.error}`, 'error');
      } else {
        showToast('Call ended for everyone', 'success');
        setOpen(false);
      }
    } catch (e) {
      showToast(`End-for-everyone failed: ${(e as Error).message}`, 'error');
    }
  };

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-12 h-12 rounded-full bg-amber-600 hover:bg-amber-700 text-white flex items-center justify-center"
        title="Moderation"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <Shield size={20} />
      </button>
      {open && (
        <div
          className="absolute bottom-14 right-0 w-64 bg-surface-800 border border-surface-600 rounded-lg shadow-xl p-2 z-50"
          role="menu"
        >
          <div className="text-xs text-text-400 px-2 py-1 uppercase tracking-wide">
            Participants
          </div>
          {participants.length === 0 && (
            <div className="text-xs text-text-500 px-2 py-2">
              No other participants.
            </div>
          )}
          {participants.map((p) => (
            <div
              key={p.userId}
              className="flex items-center justify-between px-2 py-1.5 hover:bg-surface-700 rounded"
            >
              <span className="text-sm text-text-200 truncate" title={p.displayName}>
                {p.displayName}
              </span>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => handleForceMute(p.userId, p.displayName, !p.isMuted)}
                  className="p-1 text-text-300 hover:text-amber-400"
                  title={p.isMuted ? 'Unmute' : 'Force-mute'}
                >
                  <MicOff size={14} />
                </button>
                <button
                  onClick={() => handleKick(p.userId, p.displayName)}
                  className="p-1 text-text-300 hover:text-red-400"
                  title="Remove from call"
                >
                  <UserMinus size={14} />
                </button>
              </div>
            </div>
          ))}
          {isHost && (
            <>
              <div className="border-t border-surface-600 my-1" />
              <button
                onClick={handleEndForEveryone}
                className="w-full text-left text-sm text-red-400 hover:bg-red-900/30 px-2 py-2 rounded flex items-center gap-2"
              >
                <PhoneOff size={14} />
                End call for everyone
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
};

export default HostMenu;
