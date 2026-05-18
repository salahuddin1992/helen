/**
 * ChannelTTLPanel — admin sets the per-channel auto-delete cap.
 *
 * Telegram has a similar feature ("Auto-Delete Timer"). The cap is
 * a positive number of seconds; messages older than that get
 * cleaned up by the server's hourly sweeper. Setting 0 disables
 * the feature for the channel.
 *
 * The "Sweep now" button at the bottom triggers an immediate pass
 * for *just this channel*, useful when the admin lowers the cap
 * and doesn't want to wait an hour for the periodic sweep.
 */

import React, { useEffect, useState } from 'react';
import { X, Trash2, Loader2, Check, Zap } from 'lucide-react';
import toast from 'react-hot-toast';
import { api } from '@/services/api.client';

interface Props {
  channelId: string;
  channelName?: string;
  onClose: () => void;
}

const PRESETS: Array<{ label: string; sec: number }> = [
  { label: 'إيقاف',   sec: 0 },
  { label: '1 ساعة',  sec: 3600 },
  { label: '24 ساعة', sec: 24 * 3600 },
  { label: '7 أيام',  sec: 7 * 24 * 3600 },
  { label: '14 يوم',  sec: 14 * 24 * 3600 },
  { label: '30 يوم',  sec: 30 * 24 * 3600 },
];

function fmt(sec: number): string {
  if (sec <= 0) return 'بدون';
  if (sec < 3600) return `${Math.round(sec / 60)} دقيقة`;
  if (sec < 86400) return `${Math.round(sec / 3600)} ساعة`;
  return `${Math.round(sec / 86400)} يوم`;
}

export const ChannelTTLPanel: React.FC<Props> = ({
  channelId, channelName, onClose,
}) => {
  const [current, setCurrent] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [sweeping, setSweeping] = useState(false);
  const [customRaw, setCustomRaw] = useState('');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.channelTTL.get(channelId);
        if (!cancelled) setCurrent(r.ttl_seconds);
      } catch {
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
      const r = await api.channelTTL.set(channelId, sec);
      setCurrent(r.ttl_seconds);
      toast.success(
        sec > 0
          ? `سيتم حذف الرسائل أقدم من ${fmt(sec)}`
          : 'تم إيقاف الحذف التلقائي',
      );
    } catch (e: any) {
      toast.error('فشل الحفظ: ' + (e?.message || e));
    } finally {
      setSaving(false);
    }
  };

  const applyCustomHours = async () => {
    const hours = parseFloat(customRaw);
    if (!Number.isFinite(hours) || hours < 0) {
      toast.error('أدخل عدد ساعات صحيحاً');
      return;
    }
    await apply(Math.round(hours * 3600));
  };

  const sweepNow = async () => {
    if (sweeping || saving) return;
    if (!window.confirm('تشغيل الحذف الفوري الآن؟')) return;
    setSweeping(true);
    try {
      const r = await api.channelTTL.sweepNow(channelId);
      toast.success(`حُذفت ${r.deleted} رسالة`);
    } catch (e: any) {
      toast.error('فشل: ' + (e?.message || e));
    } finally {
      setSweeping(false);
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
            <Trash2 size={16} className="text-red-400" />
            <span className="text-sm font-semibold text-gray-100">
              الحذف التلقائي
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
          >
            <X size={16} />
          </button>
        </div>

        <div className="p-4 space-y-3">
          <p className="text-xs text-gray-400">
            تحذف الرسائل الأقدم من المدة المحدّدة دوريّاً (يبدأ التطبيق
            فوريّاً، والحذف يتم كل ساعة).
          </p>

          <div className="text-[11px] text-gray-300">
            الإعداد الحالي:
            <span className="ml-1 font-semibold text-blue-300">
              {current == null ? '…' : fmt(current)}
            </span>
          </div>

          <div className="grid grid-cols-3 gap-1.5">
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
              max={720}
              step={0.5}
              value={customRaw}
              onChange={(e) => setCustomRaw(e.target.value)}
              placeholder="ساعات مخصّصة (مثلاً 12)"
              className="flex-1 px-2 py-1.5 text-xs bg-surface-800
                         border border-surface-700 rounded
                         text-gray-100 placeholder-gray-500"
            />
            <button
              onClick={applyCustomHours}
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

          {current != null && current > 0 && (
            <div className="pt-2 border-t border-surface-800">
              <button
                onClick={sweepNow}
                disabled={sweeping || saving}
                className="w-full flex items-center justify-center
                           gap-1.5 px-3 py-1.5 text-xs
                           bg-amber-700/30 text-amber-200
                           hover:bg-amber-700/50 disabled:opacity-50
                           rounded border border-amber-700/40"
              >
                {sweeping
                  ? <Loader2 size={11} className="animate-spin" />
                  : <Zap size={11} />}
                <span>حذف الآن دون انتظار</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
