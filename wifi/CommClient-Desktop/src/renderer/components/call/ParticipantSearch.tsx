/**
 * ParticipantSearch — searchable participant list overlay.
 *
 * Without this, a 200-person call shows tiles in alphabetical-ish
 * order with no fast way to find one specific person. The host
 * needs to scroll through pages of avatars to spotlight someone.
 *
 * Behaviour:
 *   - Toggle with the "people" button in the toolbar (or Ctrl+/).
 *   - Live filter as the user types (name + id substring match).
 *   - Click a result to spotlight that participant in the grid.
 *   - Sticky search input at the top so long lists stay searchable.
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Search, Users, X, Star } from 'lucide-react';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';
import { useSpotlightStore } from '@/stores/spotlight.store';
import { socketManager } from '@/services/socket.manager';

const ParticipantSearch: React.FC = () => {
  const participants = useCallStore((s) => s.participants);
  const status = useCallStore((s) => s.status);
  const callId = useCallStore((s) => s.callId);
  const hostId = useCallStore((s) => s.hostId);
  const coHostIds = useCallStore((s) => s.coHostIds);
  const me = useAuthStore((s) => s.user);
  const isHost = !!me && hostId === me.id;
  const toggleSpotlight = useSpotlightStore((s) => s.toggleSpotlight);

  const toggleCoHost = (uid: string, currently: boolean) => {
    if (!callId) return;
    const event = currently
      ? 'v2_call_cohost_remove'
      : 'v2_call_cohost_add';
    socketManager.emitNoAck(event, { call_id: callId, user_id: uid });
  };

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  // Keyboard shortcut: Ctrl+/ — chosen because it doesn't clash
  // with browser/OS shortcuts and is muscle-memory close to "find".
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key === '/') {
        e.preventDefault();
        setOpen((v) => !v);
      } else if (e.key === 'Escape' && open) {
        setOpen(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  useEffect(() => {
    if (open) {
      // Auto-focus once the panel is rendered. Defer a tick so the
      // <input> definitely exists.
      setTimeout(() => inputRef.current?.focus(), 50);
    } else {
      setQuery('');
    }
  }, [open]);

  const list = useMemo(() => {
    const arr = Object.values(participants);
    arr.sort((a: any, b: any) => {
      const an = (a.displayName || a.peerId).toLowerCase();
      const bn = (b.displayName || b.peerId).toLowerCase();
      return an.localeCompare(bn);
    });
    if (!query) return arr;
    const q = query.toLowerCase().trim();
    return arr.filter((p: any) => {
      const name = (p.displayName || '').toLowerCase();
      const id = String(p.peerId || '').toLowerCase();
      return name.includes(q) || id.includes(q);
    });
  }, [participants, query]);

  const totalCount = Object.keys(participants).length;

  if (status !== 'active' && status !== 'reconnecting') return null;

  return (
    <>
      {/* Toggle button — sits in the top-right corner. Visible only
          when the call has more than one participant (there's
          nothing to search through in a 1:1 call). */}
      {totalCount > 1 && (
        <button
          onClick={() => setOpen((v) => !v)}
          className={`fixed top-4 right-4 z-30 flex items-center gap-2
                      px-3 py-1.5 rounded-full text-xs font-medium shadow-lg
                      transition-colors ${
            open
              ? 'bg-blue-600 text-white'
              : 'bg-black/60 text-white/90 hover:bg-black/80'
          }`}
          title="بحث في المشاركين (Ctrl+/)"
        >
          <Users size={14} />
          <span>{totalCount}</span>
        </button>
      )}

      {open && (
        <div className="fixed top-16 right-4 z-30 w-80 max-h-[70vh]
                        bg-surface-900/95 border border-surface-700
                        rounded-lg shadow-2xl backdrop-blur
                        overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b border-surface-700 flex items-center gap-2">
            <Search size={14} className="text-text-400 flex-shrink-0" />
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="ابحث بالاسم..."
              className="flex-1 bg-transparent text-sm text-text-100
                         placeholder-text-500 outline-none"
            />
            <button
              onClick={() => setOpen(false)}
              className="text-text-400 hover:text-text-100"
              title="Close"
            >
              <X size={14} />
            </button>
          </div>

          <div className="overflow-y-auto divide-y divide-surface-800">
            {list.length === 0 ? (
              <div className="p-4 text-xs text-text-500 text-center">
                {query ? 'لا نتائج مطابقة' : 'لا يوجد مشاركون'}
              </div>
            ) : (
              list.map((p: any) => {
                const isCoHost = coHostIds.includes(p.peerId);
                const isMe = me?.id === p.peerId;
                const isThisHost = hostId === p.peerId;
                return (
                  <div
                    key={p.peerId}
                    className="w-full px-3 py-2 flex items-center gap-2 hover:bg-surface-800"
                  >
                    <button
                      onClick={() => {
                        toggleSpotlight(p.peerId);
                        setOpen(false);
                      }}
                      className="flex-1 flex items-center gap-2 text-start min-w-0"
                    >
                      <div className="w-8 h-8 rounded-full bg-surface-700
                                      flex items-center justify-center
                                      text-text-200 text-sm font-bold flex-shrink-0">
                        {(p.displayName || p.peerId).charAt(0).toUpperCase()}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="text-sm text-text-100 truncate flex items-center gap-1">
                          {p.displayName || p.peerId.slice(0, 12)}
                          {isThisHost && (
                            <span className="text-[9px] px-1 rounded bg-amber-500/30 text-amber-200">المضيف</span>
                          )}
                          {!isThisHost && isCoHost && (
                            <span className="text-[9px] px-1 rounded bg-emerald-500/30 text-emerald-200">مشرف</span>
                          )}
                        </div>
                        <div className="flex items-center gap-1 text-[10px] text-text-500">
                          {p.isAudioMuted && <span>🔇</span>}
                          {p.isVideoOff && <span>📷</span>}
                          {p.isSharingScreen && <span>🖥️</span>}
                          {p.isHandRaised && <span>✋</span>}
                        </div>
                      </div>
                    </button>

                    {/* Promote / demote button — host-only, never on
                        the host themselves nor on the local user. */}
                    {isHost && !isThisHost && !isMe && (
                      <button
                        onClick={() => toggleCoHost(p.peerId, isCoHost)}
                        className={`p-1 rounded ${
                          isCoHost
                            ? 'bg-emerald-600 text-white hover:bg-emerald-500'
                            : 'text-text-400 hover:text-emerald-300 hover:bg-surface-700'
                        }`}
                        title={isCoHost ? 'إزالة مشرف مساعد' : 'تعيين مشرف مساعد'}
                      >
                        <Star size={12} />
                      </button>
                    )}
                  </div>
                );
              })
            )}
          </div>

          {totalCount > list.length && query && (
            <div className="px-3 py-1.5 border-t border-surface-700
                            text-[10px] text-text-500 text-center">
              {list.length} من {totalCount} مشارك
            </div>
          )}
        </div>
      )}
    </>
  );
};

export default ParticipantSearch;
