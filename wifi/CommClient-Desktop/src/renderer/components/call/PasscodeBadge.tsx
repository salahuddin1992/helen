/**
 * PasscodeBadge — host-side toggle for setting/clearing a per-call PIN.
 *
 * Renders as a small chip in the top-left while in a call. Click to
 * open a tiny popover where the host types a PIN (4–16 chars). On
 * blur with empty value the gate is cleared; the server hashes the
 * value and stores it in-memory only.
 *
 * Non-hosts see a static "🔒 محمي" badge when the call is locked,
 * with no controls.
 */

import React, { useEffect, useRef, useState } from 'react';
import { Lock } from 'lucide-react';

/** Open-padlock — same lucide export quirk as Hand/LockOpen. */
const UnlockSvg: React.FC<{ size?: number }> = ({ size = 12 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
    <path d="M7 11V7a5 5 0 0 1 9.9-1" />
  </svg>
);
import { socketManager } from '@/services/socket.manager';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';

const PasscodeBadge: React.FC = () => {
  const callId = useCallStore((s) => s.callId);
  const status = useCallStore((s) => s.status);
  const hostId = useCallStore((s) => s.hostId);
  const me = useAuthStore((s) => s.user);
  const isHost = !!me && hostId === me.id;

  const [locked, setLocked] = useState(false);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  // Subscribe to passcode_state broadcasts.
  useEffect(() => {
    if (!callId) return;
    const off = socketManager.on('call:passcode_state', (data: any) => {
      if (data?.call_id !== callId) return;
      setLocked(!!data.locked);
    });
    return () => { try { off(); } catch { /* ignore */ } };
  }, [callId]);

  useEffect(() => {
    if (status !== 'active' && status !== 'reconnecting') {
      setOpen(false);
      setLocked(false);
      setDraft('');
    }
  }, [status]);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 50);
  }, [open]);

  if (!callId) return null;
  if (status !== 'active' && status !== 'reconnecting') return null;

  // Non-host: just a read-only indicator when the call is locked.
  if (!isHost) {
    if (!locked) return null;
    return (
      <div className="fixed top-4 left-44 z-30 px-3 py-1.5 rounded-full
                      bg-amber-500/30 text-amber-100 text-xs font-medium
                      shadow-lg flex items-center gap-1">
        <Lock size={12} />
        <span>محمي بـ PIN</span>
      </div>
    );
  }

  const apply = (value: string) => {
    socketManager.emitNoAck('v2_call_passcode_set', {
      call_id: callId,
      passcode: value,
    });
    setLocked(!!value);
    setOpen(false);
    setDraft('');
  };

  return (
    <div className="fixed top-4 left-44 z-30">
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-1 px-3 py-1.5 rounded-full
                    text-xs font-medium shadow-lg transition-colors ${
          locked
            ? 'bg-amber-500/90 text-amber-950'
            : 'bg-black/60 text-white/90 hover:bg-black/80'
        }`}
        title={locked ? 'تغيير / إلغاء PIN' : 'تعيين PIN للمكالمة'}
      >
        {locked ? <Lock size={12} /> : <UnlockSvg size={12} />}
        <span>{locked ? 'مقفل' : 'بدون قفل'}</span>
      </button>

      {open && (
        <div className="absolute mt-1 left-0 bg-surface-900/95 border border-surface-700
                        rounded-lg shadow-2xl backdrop-blur p-3 w-64">
          <label className="block text-xs text-text-300 mb-1">
            PIN المكالمة (فارغ لإلغاء القفل)
          </label>
          <input
            ref={inputRef}
            type="text"
            inputMode="numeric"
            maxLength={16}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="••••"
            className="w-full bg-surface-800 border border-surface-700
                       rounded px-2 py-1.5 text-sm text-text-100 outline-none
                       tracking-widest text-center font-mono"
            onKeyDown={(e) => {
              if (e.key === 'Enter') apply(draft);
              if (e.key === 'Escape') setOpen(false);
            }}
          />
          <div className="flex justify-end gap-1 mt-2">
            <button
              onClick={() => apply('')}
              className="text-[11px] px-2 py-0.5 rounded bg-surface-700
                         hover:bg-surface-600 text-text-200"
            >
              إلغاء القفل
            </button>
            <button
              onClick={() => apply(draft)}
              disabled={!draft.trim()}
              className="text-[11px] px-2 py-0.5 rounded bg-amber-600
                         hover:bg-amber-500 text-white disabled:opacity-40"
            >
              تطبيق
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default PasscodeBadge;
