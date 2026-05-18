/**
 * JoinByCodeDialog — paste-an-invite-code panel.
 *
 * The user can:
 *   * Paste a full URL (``https://server/join/HX-ABCD-1234``) — the
 *     code is extracted from the path tail.
 *   * Paste just the code (``HX-ABCD-1234``).
 *
 * Hits ``/api/channels/join-by-code``. On success we navigate the
 * UI into the resolved channel; on failure the server's reason
 * (expired, exhausted, revoked, not_found) is surfaced inline.
 */

import React, { useState } from 'react';
import { X, Loader2, Check } from 'lucide-react';
import toast from 'react-hot-toast';
import { api } from '@/services/api.client';
import { useChatStore } from '@/stores/chat.store.v2';

interface Props {
  onClose: () => void;
  /** Optional callback the parent can pass to navigate into the
   *  freshly-joined channel. If omitted, the chat store's
   *  ``selectChannel`` is called. */
  onJoined?: (channelId: string) => void;
}

const REASON_MESSAGES: Record<string, string> = {
  not_found: 'الرمز غير موجود',
  expired: 'انتهت صلاحية الرمز',
  revoked: 'تم إلغاء هذا الرمز',
  exhausted: 'استُنفذ عدد مرات الاستخدام',
  self_redeem_forbidden: 'لا يمكنك استخدام رمز أنشأته بنفسك',
  'not an invite code': 'هذا ليس رمز دعوة',
  'cannot invite into a DM': 'لا يمكن الدخول لمحادثة شخصية بهذه الطريقة',
};

function extractCode(input: string): string {
  const trimmed = input.trim();
  if (!trimmed) return '';
  // If it looks like a URL, take the last path segment.
  if (/^https?:\/\//i.test(trimmed)) {
    try {
      const u = new URL(trimmed);
      const parts = u.pathname.split('/').filter(Boolean);
      return parts[parts.length - 1] || '';
    } catch {
      return trimmed;
    }
  }
  // ``/join/<code>`` style relative paste.
  const m = trimmed.match(/\/join\/(.+?)(?:[/?#]|$)/);
  if (m) return m[1];
  return trimmed;
}

export const JoinByCodeDialog: React.FC<Props> = ({
  onClose,
  onJoined,
}) => {
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const setActiveChannel = useChatStore((s) => s.setActiveChannel);

  const handleJoin = async () => {
    const code = extractCode(input);
    if (!code) {
      setError('الصق الرمز أو الرابط');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await api.codes.joinChannelByCode(code);
      toast.success(
        r.already_member
          ? `أنت بالفعل عضو في ${r.channel_name}`
          : `انضممت إلى ${r.channel_name}`,
        { icon: r.already_member ? 'ℹ️' : '✅' },
      );
      if (onJoined) onJoined(r.channel_id);
      else setActiveChannel?.(r.channel_id);
      onClose();
    } catch (e: any) {
      const detail = e?.detail || e?.message || String(e);
      setError(REASON_MESSAGES[detail] || `فشل: ${detail}`);
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
          <span className="text-sm font-semibold text-gray-100">
            الانضمام عبر رمز دعوة
          </span>
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
            الصق الرمز كاملاً أو رابط الدعوة الذي وصلك:
          </p>
          <textarea
            autoFocus
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              if (error) setError(null);
            }}
            placeholder="HX-ABCD-1234 أو https://server/join/HX-ABCD-1234"
            rows={2}
            className="w-full px-3 py-2 bg-surface-800 border
                       border-surface-700 rounded font-mono text-xs
                       text-gray-100 placeholder-gray-500
                       focus:border-blue-500 focus:outline-none
                       resize-none"
          />
          {error && (
            <div className="text-xs text-red-400 bg-red-900/20
                            border border-red-800 rounded p-2">
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-sm bg-surface-700
                         hover:bg-surface-600 text-gray-200 rounded"
            >
              إلغاء
            </button>
            <button
              onClick={handleJoin}
              disabled={busy || !input.trim()}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm
                         bg-blue-700 hover:bg-blue-600 disabled:opacity-50
                         text-white rounded"
            >
              {busy
                ? <Loader2 size={14} className="animate-spin" />
                : <Check size={14} />}
              <span>انضمام</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
