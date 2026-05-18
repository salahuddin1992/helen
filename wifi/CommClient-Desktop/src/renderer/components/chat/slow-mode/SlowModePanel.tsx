/**
 * SlowModePanel — channel admin sets the seconds-per-message cap.
 *
 * Modal mounted from the channel header. Pre-set values cover the
 * common "calm a noisy channel" scenarios; the operator can also
 * type a custom value (clamped to 0..21600 by the server).
 *
 * The non-admin member never sees this panel — they get the
 * countdown rendered in MessageInput when their send is rejected
 * with ``slow_mode:<n>`` (handled in a separate file).
 */

import React, { useEffect, useState } from 'react';
import { X, Clock as Hourglass, Loader2, Check } from 'lucide-react';
import toast from 'react-hot-toast';
import { api } from '@/services/api.client';

interface Props {
  channelId: string;
  channelName?: string;
  onClose: () => void;
}

const PRESETS: Array<{ label: string; sec: number }> = [
  { label: 'إيقاف', sec: 0 },
  { label: '5 ثوانٍ', sec: 5 },
  { label: '10 ثوانٍ', sec: 10 },
  { label: '30 ثانية', sec: 30 },
  { label: '1 دقيقة', sec: 60 },
  { label: '5 دقائق', sec: 300 },
  { label: '15 دقيقة', sec: 900 },
  { label: '1 ساعة', sec: 3600 },
];

function fmtSeconds(s: number): string {
  if (s <= 0) return 'بدون';
  if (s < 60) return `${s} ثانية`;
  if (s < 3600) return `${Math.round(s / 60)} دقيقة`;
  return `${Math.round(s / 3600)} ساعة`;
}

export const SlowModePanel: React.FC<Props> = ({
  channelId, channelName, onClose,
}) => {
  const [current, setCurrent] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [customRaw, setCustomRaw] = useState<string>('');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.channelSlowMode.get(channelId);
        if (!cancelled) setCurrent(r.seconds_per_message);
      } catch (e: any) {
        if (!cancelled) setCurrent(0);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [channelId]);

  // Esc closes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const apply = async (sec: number) => {
    if (saving) return;
    setSaving(true);
    try {
      const r = await api.channelSlowMode.set(channelId, sec);
      setCurrent(r.seconds_per_message);
      toast.success(
        sec > 0
          ? `تم تفعيل البطء: ${fmtSeconds(sec)} بين الرسائل`
          : 'تم إيقاف البطء',
      );
    } catch (e: any) {
      toast.error('فشل التطبيق: ' + (e?.message || e));
    } finally {
      setSaving(false);
    }
  };

  const applyCustom = async () => {
    const sec = parseInt(customRaw, 10);
    if (!Number.isFinite(sec) || sec < 0) {
      toast.error('أدخل عدداً صحيحاً غير سالب');
      return;
    }
    await apply(Math.min(sec, 21600));
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
            <Hourglass size={16} className="text-blue-400" />
            <span className="text-sm font-semibold text-gray-100">
              وضع البطء
              {channelName && (
                <span className="text-gray-400 font-normal">
                  {' '} — {channelName}
                </span>
              )}
            </span>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-surface-700"
            aria-label="إغلاق"
          >
            <X size={16} />
          </button>
        </div>

        <div className="p-4 space-y-3">
          <p className="text-xs text-gray-400">
            يحدّ أقل وقت بين رسالتين متتاليتين لنفس المستخدم.
            المشرفون لا يتأثرون.
          </p>

          <div className="text-[11px] text-gray-300">
            الإعداد الحالي:
            <span className="ml-1 font-semibold text-blue-300">
              {current == null ? '…' : fmtSeconds(current)}
            </span>
          </div>

          <div className="grid grid-cols-2 gap-1.5">
            {PRESETS.map((p) => (
              <button
                key={p.sec}
                onClick={() => apply(p.sec)}
                disabled={saving}
                className={
                  'px-2 py-1.5 text-xs rounded text-start ' +
                  (current === p.sec
                    ? 'bg-blue-700 text-white'
                    : 'bg-surface-700 text-gray-200 hover:bg-surface-600')
                }
              >
                {p.label}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-2 pt-2 border-t
                          border-surface-800">
            <input
              type="number"
              min={0}
              max={21600}
              value={customRaw}
              onChange={(e) => setCustomRaw(e.target.value)}
              placeholder="مدة مخصّصة بالثواني…"
              className="flex-1 px-2 py-1.5 text-xs bg-surface-800
                         border border-surface-700 rounded
                         text-gray-100 placeholder-gray-500"
            />
            <button
              onClick={applyCustom}
              disabled={saving || !customRaw}
              className="flex items-center gap-1 px-3 py-1.5 text-xs
                         bg-blue-700 hover:bg-blue-600
                         disabled:opacity-50 text-white rounded"
            >
              {saving
                ? <Loader2 size={11} className="animate-spin" />
                : <Check size={11} />}
              <span>تطبيق</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
