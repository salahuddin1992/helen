/**
 * CustomEmojiAdminPanel — admin UI for managing server-wide custom
 * emoji shortcodes.
 *
 * Lists every uploaded emoji with a thumbnail + filename + size +
 * delete button, plus a small upload form (shortcode field + file
 * input) at the top.
 *
 * Server already validates everything (mime/size/uniqueness); the
 * UI just surfaces the constraint as a hint and the rejection
 * detail as an inline error on failure.
 */

import React, { useEffect, useRef, useState } from 'react';
import { Trash2, Upload, Loader2 } from 'lucide-react';
import toast from 'react-hot-toast';
import { api } from '@/services/api.client';
import { invalidateCustomEmojiCache } from '@/components/chat/emoji/CustomEmojiPicker';

interface EmojiRow {
  id: string;
  shortcode: string;
  mime: string;
  size_bytes: number;
  uploaded_at: number;
  description: string;
  url: string;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export const CustomEmojiAdminPanel: React.FC = () => {
  const [list, setList] = useState<EmojiRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [shortcode, setShortcode] = useState('');
  const [description, setDescription] = useState('');
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const refresh = async () => {
    try {
      const r = await api.customEmoji.list();
      setList(r.emoji);
    } catch (e: any) {
      toast.error('فشل التحميل: ' + (e?.message || e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const handleUpload = async () => {
    setError(null);
    if (!shortcode.trim()) {
      setError('أدخل اختصاراً (شورتكود)');
      return;
    }
    if (!pendingFile) {
      setError('اختر ملف صورة');
      return;
    }
    setUploading(true);
    try {
      await api.customEmoji.upload(shortcode.trim(), pendingFile, description);
      toast.success('تم الرفع');
      setShortcode('');
      setDescription('');
      setPendingFile(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
      // Drop the picker's session cache so the new emoji shows up
      // the next time a user opens the picker.
      invalidateCustomEmojiCache();
      refresh();
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (row: EmojiRow) => {
    if (!window.confirm(`حذف :${row.shortcode}: ؟`)) return;
    try {
      await api.customEmoji.delete(row.id);
      toast.success('حُذف');
      invalidateCustomEmojiCache();
      refresh();
    } catch (e: any) {
      toast.error('فشل الحذف: ' + (e?.message || e));
    }
  };

  return (
    <div className="bg-surface-900 border border-surface-700
                    rounded-lg p-4 space-y-4">
      <h3 className="text-sm font-semibold text-gray-100">
        الإيموجي المخصّص
      </h3>

      <div className="space-y-2 p-3 bg-surface-800 border
                      border-surface-700 rounded">
        <div className="text-[11px] text-gray-400">
          PNG / WebP / SVG / GIF حتى 256 KiB. الشورتكود:
          أحرف صغيرة، أرقام، _ أو -. مثلاً ``helen-wave``.
        </div>

        <div className="flex flex-wrap gap-2">
          <input
            type="text"
            value={shortcode}
            onChange={(e) => setShortcode(e.target.value)}
            placeholder="shortcode"
            className="flex-1 min-w-[120px] px-2 py-1.5 text-xs
                       bg-surface-900 border border-surface-700
                       rounded text-gray-100 placeholder-gray-500"
          />
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="وصف (اختياري)"
            className="flex-1 min-w-[140px] px-2 py-1.5 text-xs
                       bg-surface-900 border border-surface-700
                       rounded text-gray-100 placeholder-gray-500"
          />
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/webp,image/svg+xml,image/gif"
            onChange={(e) => setPendingFile(e.target.files?.[0] || null)}
            className="text-xs text-gray-300"
          />
          <button
            onClick={handleUpload}
            disabled={uploading || !shortcode.trim() || !pendingFile}
            className="flex items-center gap-1 px-3 py-1.5 text-xs
                       bg-blue-700 hover:bg-blue-600
                       disabled:opacity-50 text-white rounded"
          >
            {uploading
              ? <Loader2 size={11} className="animate-spin" />
              : <Upload size={11} />}
            <span>رفع</span>
          </button>
        </div>

        {error && (
          <div className="text-xs text-red-300 bg-red-900/20
                          border border-red-800 rounded p-2">
            {error}
          </div>
        )}
      </div>

      <div className="space-y-1">
        {loading && (
          <div className="text-center py-4 text-xs text-gray-400">
            جارٍ التحميل…
          </div>
        )}
        {!loading && list.length === 0 && (
          <div className="text-center py-4 text-xs text-gray-500">
            لا توجد إيموجي مخصّصة بعد
          </div>
        )}
        {list.map((e) => (
          <div
            key={e.id}
            className="flex items-center gap-3 p-2 rounded
                       hover:bg-surface-800"
          >
            <img
              src={api.customEmoji.rawUrl(e.id)}
              alt={e.shortcode}
              className="w-8 h-8 object-contain rounded
                         bg-surface-700"
            />
            <div className="flex-1 min-w-0">
              <div className="font-mono text-xs text-blue-300">
                :{e.shortcode}:
              </div>
              <div className="text-[10px] text-gray-400">
                {e.mime} · {fmtBytes(e.size_bytes)}
                {e.description ? ` · ${e.description}` : ''}
              </div>
            </div>
            <button
              onClick={() => handleDelete(e)}
              className="p-1.5 rounded text-red-400
                         hover:bg-red-700/20"
              title="حذف"
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
};
