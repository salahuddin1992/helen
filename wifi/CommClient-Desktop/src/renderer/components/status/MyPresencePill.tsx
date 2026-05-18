/**
 * MyPresencePill — the user's own presence indicator in the title bar.
 *
 * Distinct from:
 *   - The connection pill (TitleBar) — that asks "is the *server*
 *     reachable?".
 *   - ActivityStatusButton — that's a free-text status *message*.
 *
 * This pill answers: "what presence am *I* advertising right now?"
 * (online / away / busy / dnd). Clicking it opens a small dropdown
 * the user picks from; the choice is sent to the server via the
 * Socket.IO `presence_set_status` event so other peers see the new
 * dot color immediately.
 *
 * The server skips the originating socket on broadcast (skip_sid),
 * so we must keep a local optimistic copy — otherwise the pill
 * would never update for the user themselves.
 */

import React, { useEffect, useRef, useState } from 'react';
import { useAuthStore } from '@/stores/auth.store';
import { useContactsStore } from '@/stores/contacts.store';
import { usePresenceStore, type SelfPresence } from '@/stores/presence.store';
import { socketManager } from '@/services/socket.manager';
import type { UserStatus } from '@/types';

type ControllableStatus = SelfPresence;

interface PresenceOption {
  value: ControllableStatus;
  label: string;
  dotClass: string;
  textClass: string;
  hint: string;
}

const OPTIONS: PresenceOption[] = [
  {
    value: 'online',
    label: 'متاح',
    dotClass: 'bg-green-400',
    textClass: 'text-green-200',
    hint: 'الجميع يرى أنك متصل',
  },
  {
    value: 'away',
    label: 'بعيد',
    dotClass: 'bg-amber-400',
    textClass: 'text-amber-200',
    hint: 'تظهر بعيدًا — سترد لاحقًا',
  },
  {
    value: 'busy',
    label: 'مشغول',
    dotClass: 'bg-red-400',
    textClass: 'text-red-200',
    hint: 'مشغول — قد لا ترد فورًا',
  },
  {
    value: 'dnd',
    label: 'لا تزعجني',
    dotClass: 'bg-red-500',
    textClass: 'text-red-200',
    hint: 'تكتم الإشعارات — اتصلوا للضرورة فقط',
  },
];

export const MyPresencePill: React.FC = () => {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const myId = useAuthStore((s) => s.user?.id) || '';

  // Source of truth lives in `presence.store` so the TitleBar can also
  // read it (to tint the "Helen" wordmark). The store handles
  // localStorage persistence — losing the socket doesn't lose the
  // user's choice, and the value is re-asserted to the server on every
  // (re)connect below.
  const myStatus = usePresenceStore((s) => s.status);
  const setMyStatus = usePresenceStore((s) => s.setStatus);
  const [open, setOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement | null>(null);

  // NOTE: deliberately *not* auto-syncing from peerStatuses[myId]. The
  // server pushes a default of 'online' on every socket reconnect (via
  // `presence:online_list`), which would silently overwrite a user who
  // had explicitly picked 'busy' or 'dnd'. The pill is the source of
  // truth for *this* device — multi-device flip would need a different
  // signal (e.g. a dedicated `presence:my_status_changed` echo) before
  // we can auto-sync without clobbering the local choice.

  // Re-assert the persisted status after every (re)connect, otherwise
  // a server restart would silently revert everyone to the auto
  // "online" we get from the connect handshake.
  useEffect(() => {
    if (!isAuthenticated) return;
    const reassert = () => {
      try {
        socketManager.emitNoAck('presence_set_status', { status: myStatus });
      } catch { /* socket not ready yet */ }
    };
    const off = socketManager.on('connect', reassert);
    if (socketManager.isConnected() && myStatus !== 'online') reassert();
    return () => { try { off(); } catch { /* */ } };
  }, [isAuthenticated, myStatus]);

  // Click-outside to close.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!dropdownRef.current) return;
      if (!dropdownRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  if (!isAuthenticated) return null;

  const current = OPTIONS.find((o) => o.value === myStatus) || OPTIONS[0];

  const pick = (next: ControllableStatus) => {
    setMyStatus(next);  // store handles localStorage persistence
    setOpen(false);
    try {
      socketManager.emitNoAck('presence_set_status', { status: next });
      // Mirror locally in the contacts store so any list rendering my
      // own row updates immediately (the server's broadcast skips us).
      useContactsStore.setState((s) => ({
        onlineUsers: { ...s.onlineUsers, [myId]: next as UserStatus },
      }));
    } catch { /* socket not ready — value stays cached and re-sent on connect */ }
  };

  return (
    <div className="relative" style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-1.5 px-2.5 py-0.5 rounded-full
                    text-[11px] font-medium transition-colors
                    bg-surface-800 hover:bg-surface-700 border border-surface-700
                    ${current.textClass}`}
        title={`أنا ${current.label} — ${current.hint}`}
      >
        <span className="relative flex w-2 h-2">
          <span className={`relative w-2 h-2 rounded-full ${current.dotClass}`} />
          {myStatus === 'online' && (
            <span className="absolute inset-0 rounded-full bg-green-400 animate-ping opacity-60" />
          )}
        </span>
        <span>أنا {current.label}</span>
      </button>

      {open && (
        <div
          ref={dropdownRef}
          className="absolute top-full mt-1 right-0 z-50 w-44
                     bg-surface-900 border border-surface-700 rounded-lg
                     shadow-xl overflow-hidden"
        >
          <div className="px-2.5 py-1.5 text-[10px] text-gray-500 border-b border-surface-800">
            اضبط حضوري
          </div>
          {OPTIONS.map((o) => {
            const active = o.value === myStatus;
            return (
              <button
                key={o.value}
                onClick={() => pick(o.value)}
                className={`w-full flex items-center gap-2 px-2.5 py-1.5
                            text-[11px] text-start transition-colors
                            ${active
                              ? 'bg-surface-700 text-white'
                              : 'text-gray-200 hover:bg-surface-800'}`}
              >
                <span className={`w-2 h-2 rounded-full ${o.dotClass}`} />
                <span className="flex-1">{o.label}</span>
                {active && <span className="text-[10px] text-gray-400">●</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default MyPresencePill;
