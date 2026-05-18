/**
 * ScheduleMessageDialog — pick a future time, write the message,
 * fire ``api.scheduledMessages.create``.
 *
 * Three quick-presets (in 15 min / in 1 hour / tomorrow 09:00)
 * plus a custom datetime picker. The body input is the same as
 * the live composer, but capped at 10000 chars (the server's max
 * per ``message_service``).
 *
 * The server already validates that ``send_at`` is in the future;
 * we mirror that check inline for a nicer UX.
 */

import React, { useEffect, useState } from 'react';
import { X, Loader2, Send, Menu as List } from 'lucide-react';
import { ScheduledMessagesList } from './ScheduledMessagesList';

// Inline calendar icon — see MessageInput.tsx for context on
// why we don't import from lucide-react.
const Calendar: React.FC<{ size?: number; className?: string }> = ({
  size = 16, className,
}) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
       width={size} height={size} className={className} aria-hidden>
    <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
    <line x1="16" y1="2" x2="16" y2="6" />
    <line x1="8" y1="2" x2="8" y2="6" />
    <line x1="3" y1="10" x2="21" y2="10" />
  </svg>
);
import toast from 'react-hot-toast';
import { api } from '@/services/api.client';

interface Props {
  channelId: string;
  channelName?: string;
  /** Optional pre-fill for the body — used when the user pasted a
   *  draft into MessageInput then chose "Schedule" instead of
   *  Send. */
  initialContent?: string;
  onClose: () => void;
  onScheduled?: (id: string) => void;
}

function isoLocalToUTC(local: string): string {
  // ``<input type="datetime-local">`` returns wall-clock time
  // without a timezone. Treat it as the user's local zone and
  // convert to ISO with offset = "Z".
  if (!local) return '';
  const d = new Date(local);
  if (Number.isNaN(d.getTime())) return '';
  return d.toISOString();
}

function dateToInputValue(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const PRESETS: Array<{ label: string; build: () => Date }> = [
  {
    label: 'بعد 15 دقيقة',
    build: () => new Date(Date.now() + 15 * 60 * 1000),
  },
  {
    label: 'بعد ساعة',
    build: () => new Date(Date.now() + 60 * 60 * 1000),
  },
  {
    label: 'الغد 9:00 صباحاً',
    build: () => {
      const d = new Date();
      d.setDate(d.getDate() + 1);
      d.setHours(9, 0, 0, 0);
      return d;
    },
  },
  {
    label: 'بعد أسبوع',
    build: () => new Date(Date.now() + 7 * 86400 * 1000),
  },
];

export const ScheduleMessageDialog: React.FC<Props> = ({
  channelId, channelName, initialContent, onClose, onScheduled,
}) => {
  const [content, setContent] = useState(initialContent || '');
  const [whenLocal, setWhenLocal] = useState<string>(() =>
    dateToInputValue(new Date(Date.now() + 60 * 60 * 1000)),
  );
  const [busy, setBusy] = useState(false);
  const [showList, setShowList] = useState(false);

  // Esc closes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const applyPreset = (build: () => Date) => {
    setWhenLocal(dateToInputValue(build()));
  };

  const submit = async () => {
    if (busy) return;
    const trimmed = content.trim();
    if (!trimmed) {
      toast.error('اكتب نص الرسالة');
      return;
    }
    const sendAtIso = isoLocalToUTC(whenLocal);
    if (!sendAtIso) {
      toast.error('وقت غير صالح');
      return;
    }
    if (new Date(sendAtIso).getTime() <= Date.now() + 10_000) {
      toast.error('اختر وقتاً في المستقبل (≥ 10 ثوان من الآن)');
      return;
    }
    setBusy(true);
    try {
      const r = await api.scheduledMessages.create({
        channel_id: channelId,
        content: trimmed,
        send_at: sendAtIso,
      });
      toast.success('تم جدولة الرسالة');
      onScheduled?.(r.id);
      onClose();
    } catch (e: any) {
      toast.error(
        'فشل الجدولة: ' + (e?.detail || e?.message || e),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/70 flex items-center
                 justify-center p-4"
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-md bg-surface-900 rounded-xl
                   overflow-hidden border border-surface-700"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between p-3
                        border-b border-surface-800">
          <div className="flex items-center gap-2">
            <Calendar size={16} className="text-blue-400" />
            <span className="text-sm font-semibold text-gray-100">
              جدولة رسالة
              {channelName && (
                <span className="text-gray-400 font-normal">
                  {' '} → {channelName}
                </span>
              )}
            </span>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-surface-700"
          >
            <X size={16} />
          </button>
        </div>

        <div className="p-4 space-y-3">
          <div>
            <label className="block text-[11px] text-gray-400 mb-1">
              الرسالة
            </label>
            <textarea
              autoFocus
              value={content}
              onChange={(e) => setContent(e.target.value.slice(0, 10000))}
              placeholder="اكتب الرسالة المُجدولة هنا…"
              rows={4}
              className="w-full px-2 py-1.5 text-sm bg-surface-800
                         border border-surface-700 rounded
                         text-gray-100 placeholder-gray-500
                         resize-none focus:border-blue-500
                         focus:outline-none"
            />
            <div className="text-[10px] text-gray-500 text-end mt-0.5">
              {content.length}/10000
            </div>
          </div>

          <div>
            <label className="block text-[11px] text-gray-400 mb-1">
              متى ترسل؟
            </label>
            <input
              type="datetime-local"
              value={whenLocal}
              onChange={(e) => setWhenLocal(e.target.value)}
              className="w-full px-2 py-1.5 text-sm bg-surface-800
                         border border-surface-700 rounded
                         text-gray-100 focus:border-blue-500
                         focus:outline-none"
            />
            <div className="flex flex-wrap gap-1.5 mt-2">
              {PRESETS.map((p) => (
                <button
                  key={p.label}
                  type="button"
                  onClick={() => applyPreset(p.build)}
                  className="px-2 py-1 text-[11px] rounded
                             bg-surface-700 text-gray-200
                             hover:bg-surface-600"
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-2 pt-2 border-t
                          border-surface-800">
            <button
              onClick={() => setShowList(true)}
              className="flex items-center gap-1 px-2 py-1.5 text-xs
                         text-blue-300 hover:text-blue-200"
              title="عرض كل الرسائل المُجدولة"
            >
              <List size={12} />
              <span>المجدولة</span>
            </button>
            <div className="flex-1" />
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-sm bg-surface-700
                         hover:bg-surface-600 text-gray-200 rounded"
            >
              إلغاء
            </button>
            <button
              onClick={submit}
              disabled={busy || !content.trim()}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm
                         bg-blue-700 hover:bg-blue-600
                         disabled:opacity-50 text-white rounded"
            >
              {busy
                ? <Loader2 size={14} className="animate-spin" />
                : <Send size={14} />}
              <span>جدولة</span>
            </button>
          </div>
        </div>

        {showList && (
          <ScheduledMessagesList
            channelId={channelId}
            onClose={() => setShowList(false)}
          />
        )}
      </div>
    </div>
  );
};
