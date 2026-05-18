/**
 * WhisperPanel — private text channel between a participant and the
 * host (or, for the host, between the host and any single
 * participant).
 *
 * UX
 * --
 * - Non-host: a single conversation with the host. Compose box at
 *   the bottom; history scrolls.
 * - Host: dropdown to pick which participant they're whispering
 *   with, then the same compose UI. Whispers from any participant
 *   land in their own thread.
 *
 * Whispers are NOT persisted server-side (transient by design).
 * They live in-memory only for the lifetime of the call.
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Send, X, MessageCircle } from 'lucide-react';
import { socketManager } from '@/services/socket.manager';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';

interface WhisperLine {
  id: string;
  fromUserId: string;
  toUserId: string;
  text: string;
  ts: number;
  fromHost: boolean;
}

const WhisperPanel: React.FC = () => {
  const callId = useCallStore((s) => s.callId);
  const status = useCallStore((s) => s.status);
  const hostId = useCallStore((s) => s.hostId);
  const participants = useCallStore((s) => s.participants);
  const me = useAuthStore((s) => s.user);
  const isHost = !!me && hostId === me.id;

  const [open, setOpen] = useState(false);
  const [lines, setLines] = useState<WhisperLine[]>([]);
  const [draft, setDraft] = useState('');
  const [hostPickedTarget, setHostPickedTarget] = useState<string>('');
  const scrollRef = useRef<HTMLDivElement>(null);

  // Subscribe to whisper events.
  useEffect(() => {
    if (!callId) return;
    const off = socketManager.on('call:whisper', (data: any) => {
      if (data?.call_id !== callId) return;
      setLines((prev) => [...prev, {
        id: `${data.ts}-${data.from_user_id}-${Math.random().toString(36).slice(2, 6)}`,
        fromUserId: data.from_user_id,
        toUserId: data.to_user_id,
        text: data.text,
        ts: data.ts,
        fromHost: !!data.from_host,
      }].slice(-200));  // cap rolling buffer
    });
    return () => { try { off(); } catch { /* ignore */ } };
  }, [callId]);

  useEffect(() => {
    if (status !== 'active' && status !== 'reconnecting') {
      setLines([]);
      setOpen(false);
      setDraft('');
      setHostPickedTarget('');
    }
  }, [status]);

  // Auto-scroll on new lines.
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [lines, open]);

  // For the host, build a list of participants (excluding self).
  const others = useMemo(() => {
    if (!me) return [];
    return Object.values(participants)
      .filter((p: any) => p.peerId !== me.id)
      .map((p: any) => ({ id: p.peerId, name: p.displayName || p.peerId }));
  }, [participants, me]);

  // Determine the conversation to render. For the non-host this is
  // always the host thread. For the host it's the picked target
  // (defaults to the most recently active counterpart).
  const counterpart: string | null = isHost
    ? hostPickedTarget || (
        // fall back to the user who whispered most recently
        lines.length > 0
          ? lines[lines.length - 1].fromUserId === me?.id
            ? lines[lines.length - 1].toUserId
            : lines[lines.length - 1].fromUserId
          : (others[0]?.id ?? null)
      )
    : (hostId ?? null);

  const visibleLines = useMemo(() => {
    if (!counterpart || !me) return [];
    return lines.filter((l) =>
      (l.fromUserId === me.id && l.toUserId === counterpart) ||
      (l.fromUserId === counterpart && l.toUserId === me.id),
    );
  }, [lines, counterpart, me]);

  // Unread badge count: non-self whispers that aren't visible while
  // the panel is closed. Cheap approximation — count lines from
  // someone other than me when the panel is closed.
  const unreadCount = useMemo(() => {
    if (open) return 0;
    return lines.filter((l) => l.fromUserId !== me?.id).length;
  }, [open, lines, me]);

  const send = () => {
    const text = draft.trim();
    if (!text || !callId) return;
    if (isHost && !counterpart) return;  // host needs a target
    socketManager.emitNoAck('v2_call_whisper', {
      call_id: callId,
      text,
      target_user_id: isHost ? counterpart : undefined,
    });
    setDraft('');
  };

  if (!callId) return null;
  if (status !== 'active' && status !== 'reconnecting') return null;

  return (
    <>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`fixed bottom-44 right-4 z-30 flex items-center gap-1
                    px-3 py-1.5 rounded-full text-xs font-medium shadow-lg
                    transition-colors relative ${
          open ? 'bg-purple-600 text-white' : 'bg-black/60 text-white/90 hover:bg-black/80'
        }`}
        title="رسائل خاصة"
      >
        <MessageCircle size={14} />
        <span>همس</span>
        {unreadCount > 0 && (
          <span className="absolute -top-1 -right-1 min-w-[18px] h-[18px] px-1
                           rounded-full bg-red-500 text-white text-[10px]
                           font-bold flex items-center justify-center">
            {unreadCount > 9 ? '9+' : unreadCount}
          </span>
        )}
      </button>

      {open && (
        <div className="fixed top-16 right-4 bottom-44 z-30 w-80
                        bg-surface-900/95 border border-surface-700
                        rounded-lg shadow-2xl backdrop-blur
                        flex flex-col overflow-hidden">
          <div className="px-3 py-2 border-b border-surface-700 flex items-center gap-2">
            <MessageCircle size={14} className="text-purple-400" />
            <span className="flex-1 text-sm font-semibold">
              همس {isHost ? 'للمشارك' : 'للمضيف'}
            </span>
            <button
              onClick={() => setOpen(false)}
              className="text-text-400 hover:text-text-100"
            >
              <X size={14} />
            </button>
          </div>

          {isHost && (
            <div className="px-3 py-2 border-b border-surface-700">
              <select
                value={counterpart || ''}
                onChange={(e) => setHostPickedTarget(e.target.value)}
                className="w-full bg-surface-800 border border-surface-700
                           rounded px-2 py-1 text-xs text-text-100 outline-none"
              >
                <option value="" disabled>اختر مشارك...</option>
                {others.map((o) => (
                  <option key={o.id} value={o.id}>{o.name}</option>
                ))}
              </select>
            </div>
          )}

          <div ref={scrollRef} className="flex-1 overflow-y-auto p-2 space-y-1">
            {visibleLines.length === 0 ? (
              <div className="text-xs text-text-500 text-center py-6">
                {isHost && !counterpart
                  ? 'اختر مشارك للبدء'
                  : 'لا رسائل خاصة بعد'}
              </div>
            ) : (
              visibleLines.map((l) => {
                const mine = l.fromUserId === me?.id;
                return (
                  <div
                    key={l.id}
                    className={`text-xs leading-relaxed flex ${
                      mine ? 'justify-end' : 'justify-start'
                    }`}
                  >
                    <div className={`px-2 py-1 rounded-lg max-w-[80%] ${
                      mine
                        ? 'bg-purple-600 text-white'
                        : 'bg-surface-800 text-text-100'
                    }`}>
                      {l.text}
                    </div>
                  </div>
                );
              })
            )}
          </div>

          <div className="p-2 border-t border-surface-700 flex gap-1">
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && send()}
              placeholder={isHost && !counterpart ? 'اختر مشاركاً...' : 'همس...'}
              maxLength={800}
              disabled={isHost && !counterpart}
              className="flex-1 bg-surface-800 border border-surface-700
                         rounded px-2 py-1 text-sm text-text-100 outline-none
                         disabled:opacity-50"
            />
            <button
              onClick={send}
              disabled={!draft.trim() || (isHost && !counterpart)}
              className="px-3 py-1 rounded bg-purple-600 hover:bg-purple-500
                         text-white disabled:opacity-40"
            >
              <Send size={14} />
            </button>
          </div>
        </div>
      )}
    </>
  );
};

export default WhisperPanel;
