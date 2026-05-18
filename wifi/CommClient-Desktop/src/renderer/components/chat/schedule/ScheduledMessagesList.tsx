/**
 * ScheduledMessagesList — modal that lists every pending
 * scheduled message for the current user, optionally scoped to a
 * single channel.
 *
 * Each row shows the destination channel, the body preview, the
 * absolute send-at time, and two actions:
 *   * تعديل — open an inline form to edit content + send-at.
 *   * إلغاء — DELETE the schedule via the API.
 *
 * The server's status field is one of {pending, sent, cancelled,
 * failed}. We only display ``pending`` here; sent/cancelled rows
 * are uninteresting once the verdict is in.
 */

import React, { useEffect, useState } from 'react';
import { X, Edit2, Trash2, Check as Save, Loader2 } from 'lucide-react';
import toast from 'react-hot-toast';
import { api } from '@/services/api.client';
import { useChatStore } from '@/stores/chat.store.v2';

interface Row {
  id: string;
  channel_id: string;
  content: string;
  send_at: string;
  status: string;
}

interface Props {
  /** When set, only show schedules for this channel. Omit for all. */
  channelId?: string;
  onClose: () => void;
}

function fmtAbs(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function fmtRel(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now();
  if (!Number.isFinite(ms)) return '';
  if (ms < 0) return 'تأخّرت';
  const mins = Math.floor(ms / 60000);
  if (mins < 60) return `بعد ${mins} دقيقة`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `بعد ${hours} ساعة`;
  return `بعد ${Math.floor(hours / 24)} يوم`;
}

function isoToInputValue(iso: string): string {
  try {
    const d = new Date(iso);
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
      `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {
    return '';
  }
}

export const ScheduledMessagesList: React.FC<Props> = ({
  channelId, onClose,
}) => {
  const channels = useChatStore((s) => s.channels);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [editContent, setEditContent] = useState('');
  const [editWhen, setEditWhen] = useState('');
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const r = await api.scheduledMessages.list(channelId);
      const all = (r.scheduled || []) as Row[];
      setRows(all.filter((x) => x.status === 'pending'));
    } catch (e: any) {
      toast.error('فشل التحميل: ' + (e?.message || e));
      setRows([]);
    }
  };

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channelId]);

  // Esc closes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const channelName = (id: string): string => {
    const ch = channels.find((c) => c.id === id);
    if (!ch) return id.slice(0, 8);
    if (ch.type === 'group') return ch.name || 'مجموعة';
    return 'محادثة شخصية';
  };

  const beginEdit = (row: Row) => {
    setEditing(row.id);
    setEditContent(row.content);
    setEditWhen(isoToInputValue(row.send_at));
  };

  const saveEdit = async (row: Row) => {
    if (busyId) return;
    setBusyId(row.id);
    try {
      const sendAtIso = editWhen
        ? new Date(editWhen).toISOString()
        : undefined;
      await api.scheduledMessages.update(row.id, {
        content: editContent,
        send_at: sendAtIso,
      });
      toast.success('حُفظت التعديلات');
      setEditing(null);
      refresh();
    } catch (e: any) {
      toast.error('فشل الحفظ: ' + (e?.message || e));
    } finally {
      setBusyId(null);
    }
  };

  const cancelOne = async (row: Row) => {
    if (busyId) return;
    if (!window.confirm('إلغاء الجدولة؟')) return;
    setBusyId(row.id);
    try {
      await api.scheduledMessages.cancel(row.id);
      toast.success('أُلغي');
      refresh();
    } catch (e: any) {
      toast.error('فشل الإلغاء: ' + (e?.message || e));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/70 flex items-center
                 justify-center p-4"
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-xl bg-surface-900 rounded-xl
                   overflow-hidden border border-surface-700 flex flex-col"
        style={{ maxHeight: '80vh' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between p-3
                        border-b border-surface-800 flex-none">
          <span className="text-sm font-semibold text-gray-100">
            الرسائل المُجدولة
            {channelId && (
              <span className="text-gray-400 font-normal">
                {' '} ({channelName(channelId)})
              </span>
            )}
          </span>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-surface-700"
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {rows === null && (
            <div className="text-center py-6 text-xs text-gray-400">
              <Loader2
                size={14}
                className="animate-spin inline-block mr-2"
              />
              جارٍ التحميل…
            </div>
          )}
          {rows && rows.length === 0 && (
            <div className="text-center py-8 text-xs text-gray-500">
              لا توجد رسائل مُجدولة حالياً
            </div>
          )}
          {rows && rows.map((row) => (
            <div
              key={row.id}
              className="p-3 border-b border-surface-800 last:border-0
                         space-y-1.5"
            >
              <div className="flex items-center gap-2">
                <span className="text-xs text-blue-300 font-medium">
                  → {channelName(row.channel_id)}
                </span>
                <span className="text-[10px] text-gray-400">
                  {fmtRel(row.send_at)} · {fmtAbs(row.send_at)}
                </span>
                <div className="flex-1" />
                {editing === row.id ? (
                  <button
                    onClick={() => saveEdit(row)}
                    disabled={busyId === row.id}
                    className="flex items-center gap-1 px-2 py-1
                               text-[11px] bg-blue-700 hover:bg-blue-600
                               text-white rounded disabled:opacity-50"
                  >
                    {busyId === row.id
                      ? <Loader2 size={11} className="animate-spin" />
                      : <Save size={11} />}
                    <span>حفظ</span>
                  </button>
                ) : (
                  <button
                    onClick={() => beginEdit(row)}
                    className="p-1.5 rounded hover:bg-surface-700
                               text-gray-300"
                    title="تعديل"
                  >
                    <Edit2 size={12} />
                  </button>
                )}
                <button
                  onClick={() => cancelOne(row)}
                  disabled={busyId === row.id}
                  className="p-1.5 rounded hover:bg-red-700/20
                             text-red-400 disabled:opacity-50"
                  title="إلغاء"
                >
                  <Trash2 size={12} />
                </button>
              </div>

              {editing === row.id ? (
                <div className="space-y-1.5">
                  <textarea
                    value={editContent}
                    onChange={(e) =>
                      setEditContent(e.target.value.slice(0, 10000))
                    }
                    rows={3}
                    className="w-full px-2 py-1.5 text-xs
                               bg-surface-800 border
                               border-surface-700 rounded
                               text-gray-100 resize-none"
                  />
                  <input
                    type="datetime-local"
                    value={editWhen}
                    onChange={(e) => setEditWhen(e.target.value)}
                    className="px-2 py-1 text-xs bg-surface-800
                               border border-surface-700 rounded
                               text-gray-100"
                  />
                </div>
              ) : (
                <div className="text-xs text-gray-200 whitespace-pre-wrap
                                break-words">
                  {row.content || (
                    <span className="text-gray-500">
                      (بدون نص)
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
