/**
 * VideoPlayerModal — full-screen in-app video player.
 *
 * Used by VideoMessageBubble when the user clicks "Play". Lazy-loads
 * an authed blob URL from Helen-Server (so the `<video>` element
 * doesn't need to send the JWT itself), wires native HTML5 controls,
 * and adds three side-buttons:
 *
 *   * Save — write the bytes to ~/Downloads via the Electron IPC.
 *   * Open externally — hand the file to the system default player
 *     (VLC / IINA / Movies & TV) for codecs Chromium can't decode.
 *   * Close — revoke the blob URL and unmount.
 *
 * Browser-mode (no Electron): the side-buttons collapse to just
 * Close + a vanilla download anchor.
 */

import React, { useEffect, useRef, useState } from 'react';
import { X, Download, ExternalLink, Loader2 } from 'lucide-react';
import toast from 'react-hot-toast';
import {
  downloadFileToDisk,
  getMediaBlobUrl,
  openWithDefaultApp,
} from '@/services/chat-downloads';
import { isPlayableVideoInChromium, formatBytes } from './videoExt';

interface Props {
  fileId: string;
  filename: string;
  onClose: () => void;
}

export const VideoPlayerModal: React.FC<Props> = ({
  fileId,
  filename,
  onClose,
}) => {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [progress, setProgress] = useState<{
    received: number;
    total: number | null;
  } | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  const playable = isPlayableVideoInChromium(filename);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const url = await getMediaBlobUrl(fileId);
        if (!cancelled) setBlobUrl(url);
      } catch (e: any) {
        if (!cancelled) setLoadError(String(e?.message || e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [fileId]);

  // Always revoke the blob URL when the modal unmounts so we don't
  // leak handles to large video files.
  useEffect(() => {
    return () => {
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  }, [blobUrl]);

  // Esc closes the modal.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const handleSave = async () => {
    if (downloading) return;
    setDownloading(true);
    setProgress({ received: 0, total: null });
    const r = await downloadFileToDisk(fileId, filename, (p) => {
      setProgress({
        received: p.bytes_received,
        total: p.bytes_total,
      });
    });
    setDownloading(false);
    if (r.error) {
      toast.error(`فشل الحفظ: ${r.error}`);
      return;
    }
    if (r.path) {
      toast.success(`حُفظ في: ${r.path}`);
    } else {
      // Browser fallback handed the file to the browser downloader.
      toast.success('بدأ التحميل');
    }
  };

  const handleOpenExternal = async () => {
    if (downloading) return;
    setDownloading(true);
    const r = await downloadFileToDisk(fileId, filename);
    setDownloading(false);
    if (!r.path || r.error) {
      toast.error(r.error || 'فشل التحميل');
      return;
    }
    const ok = await openWithDefaultApp(r.path);
    if (!ok) {
      toast.error('تعذّر فتح الملف بالتطبيق الافتراضي');
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/90 flex items-center
                 justify-center p-4"
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-5xl max-h-[90vh] flex
                   flex-col bg-surface-900 rounded-xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-3
                        border-b border-surface-800">
          <div className="text-sm text-gray-200 truncate font-medium"
               title={filename}>
            {filename}
            {!playable && (
              <span className="ml-2 px-2 py-0.5 text-[10px]
                                rounded bg-amber-700/40 text-amber-200">
                صيغة قد لا تشتغل داخل التطبيق — استخدم "فتح خارجي"
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-surface-700"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* Video */}
        <div className="flex-1 bg-black flex items-center
                        justify-center min-h-[280px]">
          {loadError && (
            <div className="text-red-400 text-sm p-6">
              خطأ في التحميل: {loadError}
            </div>
          )}
          {!loadError && !blobUrl && (
            <div className="flex items-center gap-2 text-gray-300 text-sm">
              <Loader2 className="animate-spin" size={16} />
              <span>جارٍ تحميل الفيديو…</span>
            </div>
          )}
          {!loadError && blobUrl && (
            <video
              ref={videoRef}
              src={blobUrl}
              controls
              autoPlay
              playsInline
              className="max-h-[70vh] max-w-full"
              onError={() => setLoadError(
                'تعذّر تشغيل هذه الصيغة داخل التطبيق. استخدم زر "فتح خارجي".',
              )}
            />
          )}
        </div>

        {/* Action bar */}
        <div className="flex items-center gap-2 p-3 border-t
                        border-surface-800">
          <button
            onClick={handleSave}
            disabled={downloading}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm
                       bg-blue-700 hover:bg-blue-600 disabled:opacity-50
                       text-white rounded"
          >
            <Download size={14} />
            <span>حفظ</span>
          </button>
          <button
            onClick={handleOpenExternal}
            disabled={downloading}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm
                       bg-surface-700 hover:bg-surface-600
                       disabled:opacity-50 text-white rounded"
          >
            <ExternalLink size={14} />
            <span>فتح خارجي</span>
          </button>
          <div className="flex-1 text-xs text-gray-400 text-end">
            {downloading && progress && (
              <>
                {formatBytes(progress.received)}
                {progress.total ? ` / ${formatBytes(progress.total)}` : ''}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
