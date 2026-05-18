/**
 * LobbyPanel — host-side panel for admit/deny on knock-to-enter.
 *
 * Subscribes to ``call:lobby_knock`` socket events and lets the
 * host approve or deny each requester. Renders nothing for non-
 * hosts and when the lobby queue is empty.
 *
 * Lobby UX
 * --------
 * Server-side state:
 *   - ``v2_call_lobby_set_enabled`` flips the gate.
 *   - ``v2_call_lobby_knock`` queues a user.
 *   - ``v2_call_lobby_admit`` / ``v2_call_lobby_deny`` resolves it.
 *
 * The panel listens for ``call:lobby_knock`` and ``call:lobby_state``
 * events and keeps a local list. Decisions are routed via the
 * socket; the server fires ``call:lobby_admitted`` /
 * ``call:lobby_denied`` to the target user so their client can
 * proceed (or show a "denied" toast).
 */

import React, { useEffect, useState } from 'react';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';
import { socketManager } from '@/services/socket.manager';
import { Lock, UserCheck } from 'lucide-react';

/** Open-padlock icon (lucide-style). Inline because the bare
 *  `LockOpen` symbol isn't re-exported in our pinned lucide-react
 *  build — same gotcha as `Hand` / `BarChart3`. */
const LockOpenSvg: React.FC<{ size?: number; className?: string }> = ({
  size = 14, className,
}) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round" className={className}>
    <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
    <path d="M7 11V7a5 5 0 0 1 9.9-1" />
  </svg>
);

/** User-deny icon (X over a person silhouette). */
const UserXSvg: React.FC<{ size?: number; className?: string }> = ({
  size = 14, className,
}) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round" className={className}>
    <path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
    <circle cx="8.5" cy="7" r="4" />
    <line x1="18" y1="8" x2="23" y2="13" />
    <line x1="23" y1="8" x2="18" y2="13" />
  </svg>
);

interface PendingKnock {
  userId: string;
  displayName?: string;
  knockedAt: number;
}

const LobbyPanel: React.FC = () => {
  const callId = useCallStore((s) => s.callId);
  const hostId = useCallStore((s) => s.hostId);
  const status = useCallStore((s) => s.status);
  const me = useAuthStore((s) => s.user);

  const [enabled, setEnabled] = useState(false);
  const [pending, setPending] = useState<PendingKnock[]>([]);

  const isHost = !!me && hostId === me.id;

  // Subscribe to lobby state + knock events.
  useEffect(() => {
    if (!callId) return;
    const offState = socketManager.on('call:lobby_state', (data: any) => {
      if (data?.call_id !== callId) return;
      setEnabled(!!data.enabled);
    });
    const offKnock = socketManager.on('call:lobby_knock', (data: any) => {
      if (data?.call_id !== callId) return;
      setPending((prev) => {
        if (prev.some((p) => p.userId === data.user_id)) return prev;
        return [
          ...prev,
          {
            userId: data.user_id,
            displayName: data.display_name,
            knockedAt: Date.now(),
          },
        ];
      });
    });
    return () => {
      try { offState(); } catch { /* ignore */ }
      try { offKnock(); } catch { /* ignore */ }
    };
  }, [callId]);

  // Reset state when the call ends so a stale queue from a prior
  // call doesn't leak into the next one.
  useEffect(() => {
    if (status === 'idle' || status === 'ended') {
      setPending([]);
      setEnabled(false);
    }
  }, [status]);

  if (!callId || !isHost) return null;
  if (status !== 'active' && status !== 'reconnecting') return null;

  const toggleLobby = () => {
    socketManager.emitNoAck('v2_call_lobby_set_enabled', {
      call_id: callId,
      enabled: !enabled,
    });
    setEnabled((v) => !v);  // optimistic
  };

  const admit = (userId: string) => {
    socketManager.emitNoAck('v2_call_lobby_admit', {
      call_id: callId,
      user_id: userId,
    });
    setPending((prev) => prev.filter((p) => p.userId !== userId));
  };

  const deny = (userId: string) => {
    socketManager.emitNoAck('v2_call_lobby_deny', {
      call_id: callId,
      user_id: userId,
    });
    setPending((prev) => prev.filter((p) => p.userId !== userId));
  };

  // When the lobby is off AND no one is knocking, the panel renders
  // a single small toggle chip in the corner — minimal footprint.
  if (!enabled && pending.length === 0) {
    return (
      <button
        onClick={toggleLobby}
        className="fixed top-4 left-32 z-30 px-3 py-1.5 rounded-full
                   bg-black/60 hover:bg-black/80 text-white/90
                   text-xs font-medium shadow-lg flex items-center gap-1"
        title="تفعيل غرفة الانتظار"
      >
        <LockOpenSvg size={14} />
        <span>غرفة الانتظار</span>
      </button>
    );
  }

  return (
    <div className="fixed top-16 left-4 z-30 w-72
                    bg-surface-900/95 border border-surface-700
                    rounded-lg shadow-2xl backdrop-blur overflow-hidden flex flex-col">
      <div className={`px-3 py-2 border-b border-surface-700 flex items-center gap-2 ${
        enabled ? 'bg-blue-500/20' : ''
      }`}>
        {enabled ? (
          <Lock size={14} className="text-blue-300" />
        ) : (
          <LockOpenSvg size={14} className="text-text-400" />
        )}
        <span className="flex-1 text-sm font-semibold">
          {enabled ? 'غرفة الانتظار مفعّلة' : 'غرفة الانتظار'}
        </span>
        <button
          onClick={toggleLobby}
          className={`px-2 py-0.5 rounded text-[10px] font-bold ${
            enabled
              ? 'bg-blue-600 text-white'
              : 'bg-surface-700 text-text-300 hover:bg-surface-600'
          }`}
        >
          {enabled ? 'On' : 'Off'}
        </button>
      </div>

      {pending.length > 0 ? (
        <ul className="overflow-y-auto max-h-64 divide-y divide-surface-800">
          {pending.map((p) => (
            <li key={p.userId} className="px-3 py-2 flex items-center gap-2">
              <span className="flex-1 text-sm truncate">
                {p.displayName || p.userId.slice(0, 10)}
              </span>
              <button
                onClick={() => admit(p.userId)}
                className="p-1.5 rounded bg-green-600 hover:bg-green-500 text-white"
                title="السماح بالدخول"
              >
                <UserCheck size={14} />
              </button>
              <button
                onClick={() => deny(p.userId)}
                className="p-1.5 rounded bg-red-600 hover:bg-red-500 text-white"
                title="رفض"
              >
                <UserXSvg size={14} />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <div className="px-3 py-3 text-xs text-text-500 text-center">
          لا أحد ينتظر حالياً
        </div>
      )}
    </div>
  );
};

export default LobbyPanel;
