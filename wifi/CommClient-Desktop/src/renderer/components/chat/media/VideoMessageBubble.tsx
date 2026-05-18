/**
 * VideoMessageBubble — chat-bubble-sized preview of a video file.
 *
 * Three states the user sees:
 *   1. Idle — black tile + filename + size + ▶ Play button.
 *   2. Player open — VideoPlayerModal mounted as a sibling.
 *   3. After download — small "حُفظ" line with a "فتح" button that
 *      hands the local file path to the system default app.
 *
 * We deliberately don't auto-play in the bubble: many videos are
 * large, and Telegram-style chat scroll would aggressively decode
 * dozens at once. Click-to-play matches what the user asked for.
 */

import React, { useEffect, useState } from 'react';
import { Play, Download, Folder, Loader2 } from 'lucide-react';
import toast from 'react-hot-toast';
import { VideoPlayerModal } from './VideoPlayerModal';
import {
  downloadFileToDisk,
  openWithDefaultApp,
  revealInFolder,
} from '@/services/chat-downloads';
import { formatBytes, getExtension } from './videoExt';
import { getBaseUrl } from '@/services/api.client';

interface Props {
  fileId: string;
  filename: string;
  /** Optional poster URL — if Helen-Server generates a thumbnail
   *  for video files, pass it here. ``null`` falls back to a
   *  black tile with the play overlay. */
  posterUrl?: string | null;
  isOwn?: boolean;
}

export const VideoMessageBubble: React.FC<Props> = ({
  fileId,
  filename,
  posterUrl,
  isOwn = false,
}) => {
  const [playerOpen, setPlayerOpen] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [savedPath, setSavedPath] = useState<string | null>(null);
  const [bytesTotal, setBytesTotal] = useState<number | null>(null);
  const [progress, setProgress] = useState<number>(0);

  // Best-effort HEAD probe for file size — purely informational
  // (the bubble still works without it). 401 / 404 are silent.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(
          `${getBaseUrl()}/api/files/${fileId}`,
          { method: 'HEAD' },
        );
        const len = res.headers.get('content-length');
        if (!cancelled && len) {
          setBytesTotal(parseInt(len, 10));
        }
      } catch { /* ignore — size is decorative */ }
    })();
    return () => {
      cancelled = true;
    };
  }, [fileId]);

  const handleDownload = async () => {
    if (downloading) return;
    setDownloading(true);
    setProgress(0);
    const r = await downloadFileToDisk(fileId, filename, (p) => {
      setProgress(p.bytes_received);
      if (p.bytes_total != null) setBytesTotal(p.bytes_total);
    });
    setDownloading(false);
    if (r.error) {
      toast.error(`فشل التحميل: ${r.error}`);
      return;
    }
    if (r.path) {
      setSavedPath(r.path);
      toast.success('تم التحميل');
    } else {
      toast.success('بدأ التحميل');
    }
  };

  const handleOpen = async () => {
    if (!savedPath) return;
    const ok = await openWithDefaultApp(savedPath);
    if (!ok) toast.error('تعذّر فتح الملف');
  };

  const handleReveal = async () => {
    if (savedPath) await revealInFolder(savedPath);
  };

  const ext = getExtension(filename) || 'video';
  const sizeLabel = formatBytes(bytesTotal);

  return (
    <div
      className={
        'rounded-lg overflow-hidden border ' +
        (isOwn
          ? 'border-blue-700/50 bg-blue-900/10'
          : 'border-surface-700 bg-surface-800/40')
      }
      style={{ maxWidth: '320px' }}
    >
      {/* Poster / play tile */}
      <button
        onClick={() => setPlayerOpen(true)}
        className="relative w-full aspect-video bg-black flex
                   items-center justify-center group cursor-pointer
                   hover:bg-black/80 transition-colors"
      >
        {posterUrl && (
          <img
            src={posterUrl}
            alt=""
            className="absolute inset-0 w-full h-full object-cover"
          />
        )}
        <div className="relative z-10 flex items-center justify-center
                        w-14 h-14 rounded-full bg-black/60
                        group-hover:bg-black/80
                        ring-2 ring-white/30">
          <Play size={26} className="text-white ml-1" fill="white" />
        </div>
        <div className="absolute bottom-1 left-2 right-2 flex
                        items-center justify-between text-[11px]
                        text-white/80 z-10">
          <span className="uppercase tracking-wide">{ext}</span>
          <span>{sizeLabel}</span>
        </div>
      </button>

      {/* Filename + actions */}
      <div className="p-2 text-xs">
        <div className="text-gray-200 truncate" title={filename}>
          {filename || `video-${fileId.slice(0, 6)}`}
        </div>

        <div className="mt-1.5 flex items-center gap-2">
          {!savedPath ? (
            <button
              onClick={handleDownload}
              disabled={downloading}
              className="flex items-center gap-1 px-2 py-1
                         bg-surface-700 hover:bg-surface-600
                         disabled:opacity-50 text-white rounded
                         text-[11px]"
            >
              {downloading
                ? <Loader2 size={11} className="animate-spin" />
                : <Download size={11} />}
              <span>
                {downloading ? 'جارٍ التحميل…' : 'تحميل'}
              </span>
            </button>
          ) : (
            <>
              <button
                onClick={handleOpen}
                className="flex items-center gap-1 px-2 py-1
                           bg-blue-700 hover:bg-blue-600 text-white
                           rounded text-[11px]"
              >
                <Play size={11} />
                <span>فتح بالتطبيق الافتراضي</span>
              </button>
              <button
                onClick={handleReveal}
                className="flex items-center gap-1 px-2 py-1
                           bg-surface-700 hover:bg-surface-600
                           text-white rounded text-[11px]"
                title="عرض في المجلد"
              >
                <Folder size={11} />
              </button>
            </>
          )}
          {downloading && bytesTotal && (
            <span className="text-[10px] text-gray-400">
              {Math.round((progress / bytesTotal) * 100)}%
            </span>
          )}
        </div>
      </div>

      {playerOpen && (
        <VideoPlayerModal
          fileId={fileId}
          filename={filename}
          onClose={() => setPlayerOpen(false)}
        />
      )}
    </div>
  );
};
