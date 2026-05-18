/**
 * InviteLinkPanel — modal that mints + lists invite codes for a
 * channel. Built on the existing ``/api/me/codes`` endpoints
 * (kind=invite, target_channel_id=<channel>).
 *
 * Each code is rendered both as a short string (``HX-7F4Q-9P2K``)
 * and a fully-formed URL (``https://server/join/<code>``) the user
 * can copy or screenshot. Codes can carry a max-uses cap and an
 * expiry; when those are unset the code is single-use forever.
 *
 * Revocation is one click. The list refreshes after every mint /
 * revoke so the operator sees the current set without a hard reload.
 */

import React, { useEffect, useState } from 'react';
import {
  X,
  ExternalLink as LinkIcon,
  Plus,
  Copy,
  Trash2,
  Check,
  Loader2,
  Users,
  Clock,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { api, getBaseUrl } from '@/services/api.client';

interface CodeRecord {
  code: string;
  kind: string;
  note: string;
  max_uses: number | null;
  uses_count: number;
  ttl_sec: number | null;
  expires_at: string | null;
  target_channel_id: string | null;
  created_at: string;
}

interface Props {
  channelId: string;
  channelName?: string;
  onClose: () => void;
}

const TTL_OPTIONS: Array<{ label: string; sec: number | null }> = [
  { label: 'دائم', sec: null },
  { label: '1 ساعة', sec: 3600 },
  { label: '24 ساعة', sec: 86400 },
  { label: '7 أيام', sec: 7 * 86400 },
  { label: '30 يوم', sec: 30 * 86400 },
];

const USES_OPTIONS: Array<{ label: string; n: number | null }> = [
  { label: 'غير محدود', n: null },
  { label: 'مرة واحدة', n: 1 },
  { label: '5 استخدامات', n: 5 },
  { label: '50 استخدام', n: 50 },
];

function buildJoinUrl(code: string): string {
  // The server already serves a /join/<code> redirect that opens
  // the channel after auth. We use the active server origin so
  // copy-paste works on phones / browsers on the same LAN.
  const base = getBaseUrl().replace(/\/+$/, '');
  return `${base}/join/${encodeURIComponent(code)}`;
}

function fmtTimeLeft(expiresAt: string | null): string {
  if (!expiresAt) return 'دائم';
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (ms <= 0) return 'منتهي';
  const days = Math.floor(ms / 86_400_000);
  if (days >= 1) return `${days} يوم متبقّي`;
  const hours = Math.floor(ms / 3_600_000);
  if (hours >= 1) return `${hours} ساعة متبقّي`;
  const mins = Math.max(1, Math.floor(ms / 60_000));
  return `${mins} دقيقة متبقّي`;
}

export const InviteLinkPanel: React.FC<Props> = ({
  channelId,
  channelName,
  onClose,
}) => {
  const [codes, setCodes] = useState<CodeRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [ttlSec, setTtlSec] = useState<number | null>(86400);
  const [maxUses, setMaxUses] = useState<number | null>(null);
  const [note, setNote] = useState('');
  const [copiedCode, setCopiedCode] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const r = await api.codes.list();
      const mine = (r.codes || []).filter(
        (c: CodeRecord) =>
          c.target_channel_id === channelId && c.kind === 'invite',
      );
      setCodes(mine);
    } catch (e: any) {
      toast.error('فشل تحميل الروابط: ' + (e?.message || e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channelId]);

  // Esc closes the modal.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const handleCreate = async () => {
    if (creating) return;
    setCreating(true);
    try {
      await api.codes.create({
        kind: 'invite',
        target_channel_id: channelId,
        ttl_sec: ttlSec,
        max_uses: maxUses,
        note,
      });
      setNote('');
      await refresh();
      toast.success('تم إنشاء رابط الدعوة');
    } catch (e: any) {
      toast.error('فشل الإنشاء: ' + (e?.message || e));
    } finally {
      setCreating(false);
    }
  };

  const handleCopy = async (code: string) => {
    try {
      await navigator.clipboard.writeText(buildJoinUrl(code));
      setCopiedCode(code);
      setTimeout(() => setCopiedCode(null), 1500);
    } catch {
      toast.error('تعذّر النسخ — انسخ يدويّاً');
    }
  };

  const handleRevoke = async (code: string) => {
    if (!window.confirm('إلغاء هذا الرابط؟ لن يعمل بعد ذلك.')) return;
    try {
      await api.codes.revoke(code);
      toast.success('أُلغي الرابط');
      refresh();
    } catch (e: any) {
      toast.error('فشل الإلغاء: ' + (e?.message || e));
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/70 flex items-center
                 justify-center p-4"
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-lg bg-surface-900 rounded-xl
                   overflow-hidden border border-surface-700"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-3
                        border-b border-surface-800">
          <div className="flex items-center gap-2">
            <LinkIcon size={16} className="text-blue-400" />
            <span className="text-sm font-semibold text-gray-100">
              روابط الدعوة
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

        {/* Create form */}
        <div className="p-3 border-b border-surface-800 space-y-2">
          <div className="text-[11px] text-gray-400">
            مدّة الصلاحية
          </div>
          <div className="flex flex-wrap gap-1.5">
            {TTL_OPTIONS.map((o) => (
              <button
                key={String(o.sec)}
                onClick={() => setTtlSec(o.sec)}
                className={
                  'px-2 py-1 text-[11px] rounded ' +
                  (ttlSec === o.sec
                    ? 'bg-blue-700 text-white'
                    : 'bg-surface-700 text-gray-300 hover:bg-surface-600')
                }
              >
                {o.label}
              </button>
            ))}
          </div>

          <div className="text-[11px] text-gray-400 pt-1">
            عدد الاستخدامات
          </div>
          <div className="flex flex-wrap gap-1.5">
            {USES_OPTIONS.map((o) => (
              <button
                key={String(o.n)}
                onClick={() => setMaxUses(o.n)}
                className={
                  'px-2 py-1 text-[11px] rounded ' +
                  (maxUses === o.n
                    ? 'bg-blue-700 text-white'
                    : 'bg-surface-700 text-gray-300 hover:bg-surface-600')
                }
              >
                {o.label}
              </button>
            ))}
          </div>

          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="ملاحظة (اختياري) — مثلاً: لفريق المبيعات"
            className="w-full px-2 py-1.5 text-xs bg-surface-800
                       border border-surface-700 rounded
                       text-gray-100 placeholder-gray-500"
          />

          <button
            onClick={handleCreate}
            disabled={creating}
            className="w-full flex items-center justify-center gap-1.5
                       px-3 py-1.5 bg-blue-700 hover:bg-blue-600
                       disabled:opacity-50 text-white rounded text-sm"
          >
            {creating
              ? <Loader2 size={14} className="animate-spin" />
              : <Plus size={14} />}
            <span>إنشاء رابط</span>
          </button>
        </div>

        {/* List */}
        <div className="max-h-72 overflow-y-auto">
          {loading && (
            <div className="p-4 text-center text-xs text-gray-400">
              جارٍ التحميل…
            </div>
          )}
          {!loading && codes.length === 0 && (
            <div className="p-4 text-center text-xs text-gray-500">
              لا توجد روابط حالياً
            </div>
          )}
          {!loading && codes.map((c) => (
            <div
              key={c.code}
              className="p-3 border-b border-surface-800 last:border-0"
            >
              <div className="flex items-center gap-2 font-mono text-sm">
                <span className="text-blue-300 truncate flex-1"
                      title={buildJoinUrl(c.code)}>
                  {c.code}
                </span>
                <button
                  onClick={() => handleCopy(c.code)}
                  className="p-1 rounded hover:bg-surface-700
                             text-gray-300"
                  title="نسخ الرابط الكامل"
                >
                  {copiedCode === c.code
                    ? <Check size={13} className="text-emerald-400" />
                    : <Copy size={13} />}
                </button>
                <button
                  onClick={() => handleRevoke(c.code)}
                  className="p-1 rounded hover:bg-red-500/10
                             text-red-400"
                  title="إلغاء الرابط"
                >
                  <Trash2 size={13} />
                </button>
              </div>
              <div className="flex items-center gap-3 mt-1.5
                              text-[10px] text-gray-400">
                <span className="flex items-center gap-1">
                  <Users size={10} />
                  {c.uses_count}
                  {c.max_uses ? ` / ${c.max_uses}` : ' / ∞'}
                </span>
                <span className="flex items-center gap-1">
                  <Clock size={10} />
                  {fmtTimeLeft(c.expires_at)}
                </span>
                {c.note && (
                  <span className="truncate text-gray-500"
                        title={c.note}>
                    · {c.note}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
