/**
 * ActivityStatusPicker — popover that lets the user set their
 * custom status message, with quick presets and an expiry timer.
 *
 * Backed by ``/api/users/me/status-message`` (PUT to set, DELETE
 * to clear). The server's ``User.status`` column is presence-driven
 * (online/offline/away based on socket activity); the *message* is
 * the user-controlled overlay we expose here.
 *
 * Quick presets prepend an emoji so other users can read the state
 * at a glance without us inventing a new column on the user table.
 *
 * Lives in its own folder so the title-bar layout stays terse —
 * the title bar just imports ``ActivityStatusButton`` and renders it.
 */

import React, { useEffect, useRef, useState } from 'react';
import { X, Loader2 } from 'lucide-react';
import toast from 'react-hot-toast';
import { api } from '@/services/api.client';
import { useAuthStore } from '@/stores/auth.store';

interface Preset {
  emoji: string;
  label: string;
  text: string;
}

const PRESETS: Preset[] = [
  { emoji: '🟢', label: 'متاح',     text: '🟢 متاح' },
  { emoji: '🌙', label: 'بعيد',     text: '🌙 بعيد' },
  { emoji: '🔴', label: 'مشغول',    text: '🔴 مشغول — لا تزعجني' },
  { emoji: '📅', label: 'في اجتماع', text: '📅 في اجتماع' },
  { emoji: '🍽️', label: 'في الغداء',  text: '🍽️ في الغداء' },
  { emoji: '🏖️', label: 'إجازة',     text: '🏖️ إجازة' },
];

interface ExpiryOption {
  label: string;
  /** Seconds from now until the status auto-clears.
   *  ``null`` = no expiry. */
  seconds: number | null;
}

const EXPIRY_OPTIONS: ExpiryOption[] = [
  { label: 'بدون انتهاء', seconds: null },
  { label: '30 دقيقة',    seconds: 30 * 60 },
  { label: 'ساعة',        seconds: 60 * 60 },
  { label: '4 ساعات',     seconds: 4 * 3600 },
  { label: 'حتى الغد',    seconds: 24 * 3600 },
];

function isoSecondsFromNow(seconds: number | null): string | null {
  if (seconds == null) return null;
  return new Date(Date.now() + seconds * 1000).toISOString();
}

interface PopoverProps {
  initialText?: string;
  onClose: () => void;
}

const ActivityStatusPopover: React.FC<PopoverProps> = ({
  initialText, onClose,
}) => {
  const [text, setText] = useState(initialText || '');
  const [expiryIdx, setExpiryIdx] = useState(0);
  const [busy, setBusy] = useState(false);

  // Esc closes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const apply = async (statusText: string) => {
    if (busy) return;
    setBusy(true);
    try {
      const expiry = EXPIRY_OPTIONS[expiryIdx];
      const expires = isoSecondsFromNow(expiry.seconds);
      const r = await api.setStatusMessage(statusText.slice(0, 140), expires);
      // Keep the auth store in sync — title-bar pill reads from it.
      useAuthStore.setState((s) => ({
        user: s.user
          ? {
              ...s.user,
              status_message: r.status_message ?? statusText,
              status_expires_at:
                r.status_expires_at ?? expires ?? null,
            } as any
          : s.user,
      }));
      toast.success('تم تحديث الحالة');
      onClose();
    } catch (e: any) {
      toast.error('فشل تحديث الحالة: ' + (e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await api.clearStatusMessage();
      useAuthStore.setState((s) => ({
        user: s.user
          ? { ...s.user, status_message: null, status_expires_at: null } as any
          : s.user,
      }));
      toast.success('تم مسح الحالة');
      onClose();
    } catch (e: any) {
      toast.error('فشل المسح: ' + (e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 flex items-start
                 justify-center pt-16"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm bg-surface-900 rounded-xl
                   overflow-hidden border border-surface-700"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between p-3
                        border-b border-surface-800">
          <span className="text-sm font-semibold text-gray-100">
            حالتي
          </span>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-surface-700"
          >
            <X size={14} />
          </button>
        </div>

        <div className="p-3 space-y-3">
          {/* Presets */}
          <div className="grid grid-cols-2 gap-1.5">
            {PRESETS.map((p) => (
              <button
                key={p.label}
                onClick={() => apply(p.text)}
                disabled={busy}
                className="flex items-center gap-2 px-2 py-1.5 text-xs
                           text-start rounded bg-surface-800
                           hover:bg-surface-700 text-gray-100
                           disabled:opacity-50"
              >
                <span aria-hidden>{p.emoji}</span>
                <span>{p.label}</span>
              </button>
            ))}
          </div>

          {/* Custom text */}
          <div>
            <label className="block text-[11px] text-gray-400 mb-1">
              رسالة مخصّصة
            </label>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value.slice(0, 140))}
              placeholder="🚀 مكتب الإسبوع — رد لاحقاً"
              rows={2}
              className="w-full px-2 py-1.5 text-xs bg-surface-800
                         border border-surface-700 rounded
                         text-gray-100 placeholder-gray-500
                         resize-none focus:border-blue-500
                         focus:outline-none"
            />
            <div className="text-[10px] text-gray-500 text-end mt-0.5">
              {text.length}/140
            </div>
          </div>

          {/* Expiry */}
          <div>
            <label className="block text-[11px] text-gray-400 mb-1">
              ينتهي بعد
            </label>
            <div className="flex flex-wrap gap-1">
              {EXPIRY_OPTIONS.map((o, i) => (
                <button
                  key={o.label}
                  onClick={() => setExpiryIdx(i)}
                  className={
                    'px-2 py-0.5 text-[10px] rounded ' +
                    (expiryIdx === i
                      ? 'bg-blue-700 text-white'
                      : 'bg-surface-700 text-gray-300 hover:bg-surface-600')
                  }
                >
                  {o.label}
                </button>
              ))}
            </div>
          </div>

          {/* Actions */}
          <div className="flex gap-2 pt-2 border-t border-surface-800">
            <button
              onClick={clear}
              disabled={busy}
              className="px-3 py-1.5 text-xs bg-red-700/30 text-red-200
                         hover:bg-red-700/50 disabled:opacity-50 rounded"
            >
              مسح الحالة
            </button>
            <div className="flex-1" />
            <button
              onClick={() => apply(text)}
              disabled={busy || !text.trim()}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs
                         bg-blue-700 hover:bg-blue-600
                         disabled:opacity-50 text-white rounded"
            >
              {busy && <Loader2 size={11} className="animate-spin" />}
              <span>تطبيق</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};


export const ActivityStatusButton: React.FC = () => {
  const me = useAuthStore((s) => s.user);
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);

  if (!me) return null;
  const message = (me as any).status_message as string | null | undefined;

  // Pick a leading emoji from the message if any, otherwise show
  // a generic "set status" placeholder.
  const display = message ? message : 'حالتي';
  const truncated =
    display.length > 18 ? display.slice(0, 18) + '…' : display;

  return (
    <>
      <button
        ref={buttonRef}
        onClick={() => setOpen(true)}
        className="flex items-center gap-1.5 px-2 py-0.5 rounded-full
                   bg-surface-800 hover:bg-surface-700 text-[11px]
                   text-gray-300 transition-colors"
        style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}
        title={message || 'اضبط حالتك'}
      >
        <span>{truncated}</span>
      </button>
      {open && (
        <ActivityStatusPopover
          initialText={message || ''}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
};
